from __future__ import annotations

import argparse
import json
import sqlite3
from collections import deque
from pathlib import Path
from typing import Iterable

from .contract.graph_contract import normalize_http_route

_DB_TYPES = frozenset(
    {"TABLE", "VIEW", "MATERIALIZED_VIEW", "EXTERNAL_DATABASE_OBJECT"}
)
_UI_TYPES = frozenset(
    {
        "SCREEN",
        "ROUTE",
        "ANGULAR_COMPONENT",
        "COMPONENT",
        "UI_ACTION",
        "API_CALL_REFERENCE",
        "API_CLIENT_CALL",
    }
)
_JOB_TYPES = frozenset(
    {"JOB", "JOB_NETWORK", "EXECUTABLE", "EXECUTABLE_ENTRY_POINT", "LOADER_CONTROL"}
)


class KnowledgeStore:
    """Lazy reader for a generated graph package and its derived knowledge layers."""

    def __init__(self, output: Path) -> None:
        self.output = output.expanduser().resolve()
        self.manifest = self._load_json(self.output / "manifest.json")
        self.index = self._load_json(self.output / "graph-index.json")
        if self.index.get("indexVersion") != "2.0":
            raise ValueError(
                f"Unsupported graph index version: {self.index.get('indexVersion')!r}"
            )
        configured = str(
            self.manifest.get("files", {}).get("database")
            or self.index.get("database")
            or "graph.sqlite"
        )
        database = self.output / configured
        if not database.is_file():
            raise ValueError(f"SQLite graph database not found: {database}")
        self.database = database
        self.database_uri = f"{database.as_uri()}?mode=ro"

    def find_node(
        self,
        *,
        node_id: str = "",
        qualified_name: str = "",
        node_type: str = "",
        limit: int = 50,
    ) -> dict[str, object]:
        if node_id:
            rows = self._rows_by_identity("nodes", "node_id", node_id)
            return self._response(f"Found {len(rows)} node(s).", nodes=rows)
        elif qualified_name:
            names = self.index.get("qualifiedNames", {})
            locators = list(names.get(qualified_name.casefold(), ()))
            if not locators:
                rows = self._query_rows(
                    "SELECT * FROM nodes WHERE qualified_name LIKE ? "
                    "ORDER BY node_id LIMIT ?",
                    (f"%{qualified_name}%", limit),
                )
                return self._response(f"Found {len(rows)} node(s).", nodes=rows)
        elif node_type:
            locators = list(
                self.index.get("nodesByType", {}).get(node_type.upper(), ())
            )
        else:
            raise ValueError("findNode requires id, qualified_name, or type")
        rows = self._rows_from_locators(
            locators[:limit], "nodes", "nodeId", "node_id"
        )
        return self._response(
            f"Found {len(rows)} node(s).",
            nodes=rows,
        )

    def find_edges(
        self,
        *,
        source: str = "",
        target: str = "",
        edge_type: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        indexes = []
        if source:
            source = self._resolve_identifier("nodes", "node_id", source) or ""
            indexes.append(
                self.index.get("edgesBySource", {}).get(source, ())
            )
        if target:
            target = self._resolve_identifier("nodes", "node_id", target) or ""
            indexes.append(
                self.index.get("edgesByTarget", {}).get(target, ())
            )
        if edge_type:
            indexes.append(
                self.index.get("edgesByType", {}).get(edge_type.upper(), ())
            )
        if not indexes:
            raise ValueError("findEdges requires source, target, or type")
        locator_maps = [
            {item["edgeId"]: item for item in values} for values in indexes
        ]
        edge_ids = set(locator_maps[0])
        for mapping in locator_maps[1:]:
            edge_ids.intersection_update(mapping)
        locators = [locator_maps[0][edge_id] for edge_id in sorted(edge_ids)]
        rows = self._rows_from_locators(
            locators[:limit], "edges", "edgeId", "edge_id"
        )
        return self._response(f"Found {len(rows)} edge(s).", edges=rows)

    def find_evidence(self, target_id: str, *, limit: int = 100) -> dict[str, object]:
        node_id = self._resolve_identifier("nodes", "node_id", target_id)
        edge_id = self._resolve_identifier("edges", "edge_id", target_id)
        clauses = []
        parameters: list[object] = []
        if node_id:
            clauses.append("(target_type = 'NODE' AND target_id = ?)")
            parameters.append(int(node_id))
        if edge_id:
            clauses.append("(target_type = 'EDGE' AND target_id = ?)")
            parameters.append(int(edge_id))
        rows = (
            self._query_rows(
                "SELECT * FROM evidence WHERE "
                + " OR ".join(clauses)
                + " ORDER BY evidence_id LIMIT ?",
                (*parameters, limit),
            )
            if clauses
            else []
        )
        return self._response(
            f"Found {len(rows)} evidence row(s).", evidence=rows
        )

    def open_source(self, evidence_id: str) -> dict[str, object]:
        rows = self._rows_by_identity("evidence", "evidence_id", evidence_id)
        if not rows:
            return self._response("Evidence not found.")
        row = rows[0]
        location = {
            "evidence_id": row.get("evidence_id", evidence_id),
            "source_path": row.get("source_path", ""),
            "start_line": _optional_int(row.get("start_line", "")),
            "end_line": _optional_int(row.get("end_line", "")),
            "start_column": _optional_int(row.get("start_column", "")),
            "end_column": _optional_int(row.get("end_column", "")),
            "snippet": row.get("snippet", ""),
        }
        return self._response(
            f"Open {location['source_path']} at line {location['start_line'] or 1}.",
            evidence=rows,
            source_locations=[location],
        )

    def impact_api(self, method: str, path: str) -> dict[str, object]:
        verb, route = normalize_http_route(method, path)
        api_index = self.index.get("apisByMethodPath", {})
        locators = list(api_index.get(f"{verb} {route}", ()))
        if not locators:
            locators = [
                locator
                for signature, values in api_index.items()
                if signature.startswith(verb + " ")
                and _route_matches(signature.split(" ", 1)[1], route)
                for locator in values
            ]
        starts = self._rows_from_locators(
            locators, "nodes", "nodeId", "node_id"
        )
        return self._trace_response(
            starts,
            direction="out",
            target_types=_DB_TYPES,
            answer_prefix=f"Impact for {verb} {route}",
        )

    def impact_table(
        self, database: str, schema: str, table: str
    ) -> dict[str, object]:
        needle = ".".join(
            value for value in (database, schema, table) if value
        ).upper()
        table_locators = []
        for qualified, values in self.index.get("tablesByQName", {}).items():
            if (
                qualified == needle
                or qualified.endswith("." + needle)
                or qualified.endswith(":" + needle.replace(".", ":"))
            ):
                table_locators.extend(values)
        starts = self._rows_from_locators(
            table_locators, "nodes", "nodeId", "node_id"
        )
        return self._trace_response(
            starts,
            direction="in",
            target_types=(
                frozenset(
                    {
                        "API_OPERATION",
                        "SQL_FILE",
                        "PROCEDURE",
                        "FUNCTION",
                        "LOCAL_ROUTINE",
                        "METHOD",
                        "REPOSITORY",
                        "INLINE_SQL",
                        "SQL_STATEMENT",
                        "MAPPER_STATEMENT",
                    }
                )
                | _UI_TYPES
                | _JOB_TYPES
            ),
            answer_prefix=f"Impact for table {needle}",
        )

    def trace_ui_to_db(self, query: str) -> dict[str, object]:
        candidates = self.find_node(qualified_name=query, limit=20)["data"].get(
            "nodes", []
        )
        if not candidates and " " in query:
            method, path = query.split(" ", 1)
            try:
                verb, route = normalize_http_route(method, path)
            except ValueError:
                pass
            else:
                locators = self.index.get("apisByMethodPath", {}).get(
                    f"{verb} {route}", ()
                )
                candidates = self._rows_from_locators(
                    locators, "nodes", "nodeId", "node_id"
                )
        return self._trace_response(
            list(candidates),
            direction="out",
            target_types=_DB_TYPES,
            answer_prefix=f"UI/API to database trace for {query}",
        )

    def list_issues(
        self,
        *,
        source: str = "",
        issue_type: str = "",
        severity: str = "",
        limit: int = 200,
    ) -> dict[str, object]:
        clauses = []
        parameters: list[object] = []
        if source:
            clauses.append("package_key = ?")
            parameters.append(f"sources/{source}")
        if issue_type:
            clauses.append("issue_type = ? COLLATE NOCASE")
            parameters.append(issue_type)
        if severity:
            clauses.append("severity = ? COLLATE NOCASE")
            parameters.append(severity)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._query_rows(
            f"SELECT * FROM issues{where} ORDER BY issue_id LIMIT ?",
            (*parameters, limit),
        )
        return self._response(
            f"Found {len(rows)} issue(s).", issues=rows
        )

    def explain_node(self, node_id: str) -> dict[str, object]:
        node_response = self.find_node(node_id=node_id)
        nodes = node_response["data"].get("nodes", [])
        if not nodes:
            return self._response("Node not found.")
        resolved_id = nodes[0]["node_id"]
        outgoing = self.find_edges(source=resolved_id)["data"].get("edges", [])
        incoming = self.find_edges(target=resolved_id)["data"].get("edges", [])
        evidence = self.find_evidence(resolved_id)["evidence_refs"]
        memory = self._memory_record(resolved_id)
        return self._response(
            f"Node {node_id} has {len(outgoing)} outgoing and {len(incoming)} incoming edge(s).",
            nodes=nodes,
            edges=[*outgoing, *incoming],
            evidence=evidence,
            extra={"memory": memory},
        )

    def search_memory(
        self,
        text: str,
        *,
        kind: str = "",
        source: str = "",
        limit: int = 50,
    ) -> dict[str, object]:
        manifest = self._load_json(self.output / "codebase-memory" / "manifest.json")
        files = manifest.get("files", {})
        paths = [
            *files.get("entities", []),
            *files.get("relationships", []),
        ]
        needle = text.casefold()
        matches = []
        for relative in paths:
            path = self.output / "codebase-memory" / relative
            if not path.is_file():
                continue
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    haystack = json.dumps(row, ensure_ascii=False).casefold()
                    if needle and needle not in haystack:
                        continue
                    if kind and str(row.get("kind", "")).casefold() != kind.casefold():
                        continue
                    if source and str(row.get("source", "")).casefold() != source.casefold():
                        continue
                    matches.append(row)
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        return self._response(
            f"Found {len(matches)} memory card(s).",
            extra={"memory": matches},
        )

    def health(self) -> dict[str, object]:
        if not self._table_exists("quality_metrics"):
            return self._response(
                "V3 quality metrics are unavailable; rebuild with Extractor V3."
            )
        rows = self._query_rows(
            "SELECT * FROM quality_metrics "
            "ORDER BY scope_type, scope_key, metric_name"
        )
        global_metrics = {
            row["metric_name"]: _numeric_value(row["metric_value"])
            for row in rows
            if row["scope_type"] == "GLOBAL" and row["scope_key"] == "global"
        }
        return self._response(
            f"Loaded {len(rows)} V3 quality metric(s).",
            extra={"metrics": global_metrics, "metric_rows": rows},
        )

    def input_output(self, node_id: str) -> dict[str, object]:
        if not self._table_exists("io_items"):
            return self._response(
                "V3 input/output data is unavailable; rebuild with Extractor V3."
            )
        resolved = self._resolve_identifier("nodes", "node_id", node_id)
        if not resolved:
            return self._response("Node not found.")
        items = self._query_rows(
            "SELECT * FROM io_items WHERE owner_node_id = ? "
            "ORDER BY direction, ordinal, io_id",
            (int(resolved),),
        )
        links = self._query_rows(
            "SELECT l.*, n.stable_id AS target_stable_id, "
            "n.node_type AS target_node_type, n.qualified_name AS target_qualified_name "
            "FROM io_links l JOIN nodes n ON n.node_id = l.target_node_id "
            "WHERE l.io_id IN (SELECT io_id FROM io_items WHERE owner_node_id = ?) "
            "ORDER BY l.io_id, l.io_link_id",
            (int(resolved),),
        )
        return self._response(
            f"Found {len(items)} input/output item(s) for node {node_id}.",
            extra={"io_items": items, "io_links": links},
        )

    def trace_flow(self, node_id: str, *, limit: int = 100) -> dict[str, object]:
        if not self._table_exists("flows"):
            return self._response(
                "V3 materialized flows are unavailable; rebuild with Extractor V3."
            )
        resolved = self._resolve_identifier("nodes", "node_id", node_id)
        if not resolved:
            return self._response("Node not found.")
        flows = self._query_rows(
            "SELECT DISTINCT f.* FROM flows f "
            "LEFT JOIN flow_steps s ON s.flow_id = f.flow_id "
            "WHERE f.start_node_id = ? OR f.target_node_id = ? OR s.node_id = ? "
            "ORDER BY f.flow_id LIMIT ?",
            (int(resolved), int(resolved), int(resolved), limit),
        )
        flow_ids = [int(row["flow_id"]) for row in flows]
        steps: list[dict[str, str]] = []
        flow_io: list[dict[str, str]] = []
        if flow_ids:
            placeholders = ",".join("?" for _ in flow_ids)
            steps = self._query_rows(
                "SELECT s.*, n.stable_id AS node_stable_id, "
                "n.node_type, n.qualified_name "
                "FROM flow_steps s JOIN nodes n ON n.node_id = s.node_id "
                f"WHERE s.flow_id IN ({placeholders}) "
                "ORDER BY s.flow_id, s.step_index",
                tuple(flow_ids),
            )
            flow_io = self._query_rows(
                "SELECT fi.*, i.stable_id AS io_stable_id, i.kind, i.name, "
                "i.owner_node_id FROM flow_io fi "
                "JOIN io_items i ON i.io_id = fi.io_id "
                f"WHERE fi.flow_id IN ({placeholders}) "
                "ORDER BY fi.flow_id, fi.direction, fi.io_id",
                tuple(flow_ids),
            )
        return self._response(
            f"Found {len(flows)} materialized flow(s) for node {node_id}.",
            extra={"flows": flows, "flow_steps": steps, "flow_io": flow_io},
        )

    def unresolved(self, *, limit: int = 200) -> dict[str, object]:
        nodes = self._query_rows(
            "SELECT * FROM nodes WHERE node_type IN "
            "('UNRESOLVED_REFERENCE', 'EXTERNAL_DATABASE_OBJECT') "
            "ORDER BY node_id LIMIT ?",
            (limit,),
        )
        candidates: list[dict[str, str]] = []
        if self._table_exists("resolution_candidates"):
            candidates = self._query_rows(
                "SELECT r.*, n.stable_id AS candidate_stable_id, "
                "n.node_type AS candidate_node_type, "
                "n.qualified_name AS candidate_qualified_name "
                "FROM resolution_candidates r "
                "JOIN nodes n ON n.node_id = r.candidate_node_id "
                "ORDER BY r.reference_node_id, r.rank LIMIT ?",
                (limit * 10,),
            )
        return self._response(
            f"Found {len(nodes)} unresolved/external reference node(s).",
            nodes=nodes,
            extra={"resolution_candidates": candidates},
        )

    def catalog_status(self) -> dict[str, object]:
        if not self._table_exists("catalog_files"):
            return self._response(
                "V3 catalog metadata is unavailable; no catalog was published."
            )
        rows = self._query_rows(
            "SELECT * FROM catalog_files ORDER BY path"
        )
        return self._response(
            f"Found {len(rows)} catalog file record(s).",
            extra={"catalog_files": rows},
        )

    def _trace_response(
        self,
        starts: list[dict[str, str]],
        *,
        direction: str,
        target_types: frozenset[str],
        answer_prefix: str,
        max_depth: int = 8,
    ) -> dict[str, object]:
        start_ids = [row["node_id"] for row in starts]
        nodes = {row["node_id"]: row for row in starts}
        edges: dict[str, dict[str, str]] = {}
        targets: set[str] = set()
        queue = deque((node_id, 0) for node_id in start_ids)
        visited = set(start_ids)
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            response = (
                self.find_edges(source=current)
                if direction == "out"
                else self.find_edges(target=current)
            )
            for edge in response["data"].get("edges", []):
                next_id = (
                    edge.get("target_node_id", "")
                    if direction == "out"
                    else edge.get("source_node_id", "")
                )
                if not next_id:
                    continue
                edges[edge["edge_id"]] = edge
                node_rows = self.find_node(node_id=next_id)["data"].get("nodes", [])
                if not node_rows:
                    continue
                nodes[next_id] = node_rows[0]
                if _matches_target(node_rows[0], target_types):
                    targets.add(next_id)
                if next_id not in visited:
                    visited.add(next_id)
                    queue.append((next_id, depth + 1))
        evidence = []
        for target_id in [*nodes, *edges]:
            evidence.extend(
                self.find_evidence(target_id)["evidence_refs"]
            )
        return self._response(
            f"{answer_prefix}: {len(starts)} start node(s), {len(targets)} impacted target(s).",
            nodes=list(nodes.values()),
            edges=list(edges.values()),
            evidence=_dedupe_rows(evidence, "evidence_id"),
            extra={
                "start_node_ids": start_ids,
                "target_node_ids": sorted(targets, key=int),
                "start_stable_ids": [
                    nodes[node_id].get("stable_id", "")
                    for node_id in start_ids
                    if node_id in nodes
                ],
                "target_stable_ids": sorted(
                    nodes[node_id].get("stable_id", "")
                    for node_id in targets
                    if node_id in nodes
                ),
            },
        )

    def _response(
        self,
        answer: str,
        *,
        nodes: list[dict[str, str]] | None = None,
        edges: list[dict[str, str]] | None = None,
        evidence: list[dict[str, str]] | None = None,
        source_locations: list[dict[str, object]] | None = None,
        issues: list[dict[str, str]] | None = None,
        extra: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nodes = nodes or []
        edges = edges or []
        evidence = evidence or []
        issues = issues or []
        confidence_values = [
            float(row.get("confidence") or 0.0) for row in [*nodes, *edges]
        ]
        locations = source_locations or [
            {
                "evidence_id": row.get("evidence_id", ""),
                "source_path": row.get("source_path", ""),
                "start_line": _optional_int(row.get("start_line", "")),
                "end_line": _optional_int(row.get("end_line", "")),
            }
            for row in evidence
        ]
        data: dict[str, object] = {
            "nodes": nodes,
            "edges": edges,
        }
        if extra:
            data.update(extra)
        return {
            "answer": answer,
            "node_ids": [row["node_id"] for row in nodes],
            "edge_ids": [row["edge_id"] for row in edges],
            "stable_node_ids": [row.get("stable_id", "") for row in nodes],
            "stable_edge_ids": [row.get("stable_id", "") for row in edges],
            "evidence_refs": evidence,
            "source_locations": locations,
            "confidence": min(confidence_values) if confidence_values else None,
            "issues": issues,
            "data": data,
        }

    def _rows_from_locators(
        self,
        locators: Iterable[dict[str, str]],
        kind: str,
        locator_id: str,
        row_id: str,
    ) -> list[dict[str, str]]:
        wanted = set()
        for locator in locators:
            identity = locator.get(locator_id)
            if identity:
                wanted.add(identity)
        if not wanted:
            return []
        table = _sqlite_table(kind)
        column = _sqlite_identity_column(row_id)
        placeholders = ",".join("?" for _ in wanted)
        return self._query_rows(
            f"SELECT * FROM {table} WHERE {column} IN ({placeholders}) "
            f"ORDER BY {column}",
            tuple(int(value) for value in sorted(wanted, key=int)),
        )

    def _rows_by_identity(
        self, table: str, identity_column: str, identity: str
    ) -> list[dict[str, str]]:
        table = _sqlite_table(table)
        identity_column = _sqlite_identity_column(identity_column)
        if identity.isdigit():
            return self._query_rows(
                f"SELECT * FROM {table} WHERE {identity_column} = ?",
                (int(identity),),
            )
        return self._query_rows(
            f"SELECT * FROM {table} WHERE stable_id = ?", (identity,)
        )

    def _resolve_identifier(
        self, table: str, identity_column: str, identity: str
    ) -> str | None:
        rows = self._rows_by_identity(table, identity_column, identity)
        return rows[0].get(identity_column) if rows else None

    def _query_rows(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> list[dict[str, str]]:
        connection = sqlite3.connect(self.database_uri, uri=True)
        try:
            connection.row_factory = sqlite3.Row
            return [
                _sqlite_row(row)
                for row in connection.execute(statement, parameters)
            ]
        finally:
            connection.close()

    def _table_exists(self, table: str) -> bool:
        connection = sqlite3.connect(self.database_uri, uri=True)
        try:
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() is not None
        finally:
            connection.close()

    def _memory_record(self, node_id: str) -> dict[str, object] | None:
        locator = self.index.get("memoryByNodeId", {}).get(node_id)
        if not isinstance(locator, dict):
            return None
        path = self.output / str(locator.get("path", ""))
        line_number = int(locator.get("line") or 0)
        if not path.is_file() or line_number < 1:
            return None
        with path.open(encoding="utf-8") as handle:
            for current, line in enumerate(handle, 1):
                if current == line_number:
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _load_json(path: Path) -> dict[str, object]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Unable to read generated package file: {path}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"Expected JSON object: {path}")
        return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query generated code-tree graph, memory, and evidence."
    )
    parser.add_argument("--output", required=True, help="Generated output directory.")
    commands = parser.add_subparsers(dest="command", required=True)

    find_node = commands.add_parser("find-node", aliases=["findNode"])
    find_node.add_argument("--id", default="")
    find_node.add_argument("--qualified-name", default="")
    find_node.add_argument("--type", default="")

    find_edges = commands.add_parser("find-edges", aliases=["findEdges"])
    find_edges.add_argument("--source", default="")
    find_edges.add_argument("--target", default="")
    find_edges.add_argument("--type", default="")

    find_evidence = commands.add_parser("find-evidence", aliases=["findEvidence"])
    find_evidence.add_argument("--target-id", required=True)

    open_source = commands.add_parser("open-source", aliases=["openSource"])
    open_source.add_argument("--evidence-id", required=True)

    impact_api = commands.add_parser("impact-api", aliases=["impactApi"])
    impact_api.add_argument("--method", required=True)
    impact_api.add_argument("--path", required=True)

    impact_table = commands.add_parser("impact-table", aliases=["impactTable"])
    impact_table.add_argument("--database", default="")
    impact_table.add_argument("--schema", default="")
    impact_table.add_argument("--table", required=True)

    trace = commands.add_parser("trace-ui-to-db", aliases=["traceUiToDb"])
    trace.add_argument("--query", required=True)

    list_issues = commands.add_parser("list-issues", aliases=["listIssues"])
    list_issues.add_argument("--source", default="")
    list_issues.add_argument("--issue-type", default="")
    list_issues.add_argument("--severity", default="")

    explain = commands.add_parser("explain-node", aliases=["explainNode"])
    explain.add_argument("--node-id", required=True)

    search = commands.add_parser("search-memory", aliases=["searchMemory"])
    search.add_argument("--text", default="")
    search.add_argument("--kind", default="")
    search.add_argument("--source", default="")

    commands.add_parser("health")

    input_output = commands.add_parser("input-output", aliases=["inputOutput"])
    input_output.add_argument("--node-id", required=True)

    trace_flow = commands.add_parser("trace-flow", aliases=["traceFlow"])
    trace_flow.add_argument("--node-id", required=True)

    commands.add_parser("unresolved")
    commands.add_parser("catalog-status", aliases=["catalogStatus"])

    args = parser.parse_args(argv)
    try:
        store = KnowledgeStore(Path(args.output))
        if args.command in {"find-node", "findNode"}:
            result = store.find_node(
                node_id=args.id,
                qualified_name=args.qualified_name,
                node_type=args.type,
            )
        elif args.command in {"find-edges", "findEdges"}:
            result = store.find_edges(
                source=args.source, target=args.target, edge_type=args.type
            )
        elif args.command in {"find-evidence", "findEvidence"}:
            result = store.find_evidence(args.target_id)
        elif args.command in {"open-source", "openSource"}:
            result = store.open_source(args.evidence_id)
        elif args.command in {"impact-api", "impactApi"}:
            result = store.impact_api(args.method, args.path)
        elif args.command in {"impact-table", "impactTable"}:
            result = store.impact_table(args.database, args.schema, args.table)
        elif args.command in {"trace-ui-to-db", "traceUiToDb"}:
            result = store.trace_ui_to_db(args.query)
        elif args.command in {"list-issues", "listIssues"}:
            result = store.list_issues(
                source=args.source,
                issue_type=args.issue_type,
                severity=args.severity,
            )
        elif args.command in {"explain-node", "explainNode"}:
            result = store.explain_node(args.node_id)
        elif args.command == "health":
            result = store.health()
        elif args.command in {"input-output", "inputOutput"}:
            result = store.input_output(args.node_id)
        elif args.command in {"trace-flow", "traceFlow"}:
            result = store.trace_flow(args.node_id)
        elif args.command == "unresolved":
            result = store.unresolved()
        elif args.command in {"catalog-status", "catalogStatus"}:
            result = store.catalog_status()
        else:
            result = store.search_memory(
                args.text, kind=args.kind, source=args.source
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _optional_int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _numeric_value(value: str) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    return int(parsed) if parsed.is_integer() else parsed


def _sqlite_table(value: str) -> str:
    if value not in {"nodes", "edges", "evidence", "comments", "issues"}:
        raise ValueError(f"Unsupported SQLite graph table: {value}")
    return value


def _sqlite_identity_column(value: str) -> str:
    if value not in {
        "node_id",
        "edge_id",
        "evidence_id",
        "comment_id",
        "issue_id",
    }:
        raise ValueError(f"Unsupported SQLite identity column: {value}")
    return value


def _sqlite_row(row: sqlite3.Row) -> dict[str, str]:
    return {
        key: "" if row[key] is None else str(row[key])
        for key in row.keys()
    }


def _dedupe_rows(
    rows: Iterable[dict[str, str]], identity: str
) -> list[dict[str, str]]:
    return list({row.get(identity, ""): row for row in rows}.values())


def _route_matches(template: str, actual: str) -> bool:
    template_parts = [part for part in template.strip("/").split("/") if part]
    actual_parts = [part for part in actual.strip("/").split("/") if part]
    return len(template_parts) == len(actual_parts) and all(
        left == right or left == "{id}"
        for left, right in zip(template_parts, actual_parts)
    )


def _matches_target(
    node: dict[str, str], target_types: frozenset[str]
) -> bool:
    if node.get("node_type") in target_types:
        return True
    if target_types != _DB_TYPES or node.get("node_type") != "UNRESOLVED_REFERENCE":
        return False
    try:
        properties = json.loads(node.get("properties_json") or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(properties, dict) and bool(properties.get("table"))


if __name__ == "__main__":
    raise SystemExit(main())
