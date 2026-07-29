from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from code_tree_exporter.contract.graph_contract import canonical_edge_id
from code_tree_exporter.graph_package import GraphPackage, SourceRecord
from code_tree_exporter.query import KnowledgeStore, main as query_main


class KnowledgeOutputTests(unittest.TestCase):
    def test_sqlite_ids_are_compact_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mappings = []
            for name in ("first", "second"):
                output = root / name
                _sample_graph().write(
                    output,
                    source_name="order-system",
                    config_path="extractor.json",
                    output_mode="partitioned",
                )
                rows = _db_rows(output, "nodes")
                mappings.append(
                    {row["stable_id"]: row["node_id"] for row in rows}
                )
                self.assertTrue(
                    all(
                        value.isdigit() and len(value) <= 19
                        for value in mappings[-1].values()
                    )
                )
                connection = sqlite3.connect(output / "graph.sqlite")
                try:
                    self.assertEqual(
                        list(connection.execute("PRAGMA foreign_key_check")), []
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM nodes WHERE stable_id = ?",
                            ("api-operation:api:GET:/orders/{id}",),
                        ).fetchone()[0],
                        1,
                    )
                finally:
                    connection.close()
                published_text = "\n".join(
                    path.read_text(encoding="utf-8")
                    for path in output.rglob("*")
                    if path.is_file()
                    and path.suffix in {".json", ".jsonl", ".md"}
                )
                self.assertNotIn(
                    "api-operation:api:GET:/orders/{id}", published_text
                )
            self.assertEqual(mappings[0], mappings[1])

    def test_semantic_duplicates_are_collapsed_and_references_are_rewritten(self) -> None:
        graph = GraphPackage()
        graph.register_source("api", "dotnet-api", "api", "api")
        owner = "method:api:owner"
        duplicate_a = "inline-sql:owner:a"
        duplicate_b = "inline-sql:owner:b"
        reference_holder = "method:api:reference-holder"
        graph.nodes[owner] = _node(owner, "METHOD", "Api.Owner", "api", {})
        graph.nodes[duplicate_a] = _node(
            duplicate_a,
            "INLINE_SQL",
            "Api.Owner.inline-sql.10",
            "api",
            {"sql": "SELECT * FROM ORDERS", "source": "declaration"},
            database="DB",
        )
        graph.nodes[duplicate_b] = _node(
            duplicate_b,
            "INLINE_SQL",
            "Api.Owner.inline-sql.10",
            "api",
            {"sql": "SELECT * FROM ORDERS", "dynamic": False},
            database="DB",
        )
        graph.nodes[duplicate_a]["graph_role"] = "EVIDENCE"
        graph.nodes[duplicate_b]["graph_role"] = "MAIN"
        graph.nodes[reference_holder] = _node(
            reference_holder,
            "METHOD",
            "Api.ReferenceHolder",
            "api",
            {"referenced_node_id": duplicate_b},
        )
        for signature in ("NUMBER", "VARCHAR2"):
            routine_id = f"local-routine:DB:PKG:OUTER:RUN:{signature}"
            graph.nodes[routine_id] = _node(
                routine_id,
                "LOCAL_ROUTINE",
                "DB.PKG.OUTER.RUN",
                "api",
                {"signature": signature},
                database="DB",
            )

        duplicate_edges = []
        for target in (duplicate_a, duplicate_b):
            edge_id = canonical_edge_id(
                owner, "CONTAINS", target, "", "STRUCTURAL"
            )
            graph.edges[edge_id] = {
                "edge_id": edge_id,
                "source_node_id": owner,
                "target_node_id": target,
                "edge_type": "CONTAINS",
                "graph_layer": "STRUCTURAL",
                "raw_operation": "",
                "confidence": "1.0",
                "properties_json": "{}",
            }
            duplicate_edges.append(edge_id)

        for suffix, target_type, target_id, kind in (
            ("node-a", "NODE", duplicate_a, "INLINE_SQL"),
            ("node-b", "NODE", duplicate_b, "INLINE_SQL"),
            ("edge-a", "EDGE", duplicate_edges[0], "REFERENCE"),
            ("edge-b", "EDGE", duplicate_edges[1], "REFERENCE"),
        ):
            evidence_id = f"evidence:{suffix}"
            graph.evidence[evidence_id] = {
                "evidence_id": evidence_id,
                "target_type": target_type,
                "target_id": target_id,
                "source_path": "api/Owner.cs",
                "start_line": "10",
                "end_line": "10",
                "start_column": "1",
                "end_column": "30",
                "evidence_kind": kind,
                "extractor_name": "test",
                "confidence": "1.0",
                "snippet": "SELECT * FROM ORDERS",
                "properties_json": "{}",
            }

        for suffix, owner_id in (("a", duplicate_a), ("b", duplicate_b)):
            comment_id = f"comment:{suffix}"
            graph.comments[comment_id] = {
                "comment_id": comment_id,
                "source_path": "api/Owner.cs",
                "owner_node_id": owner_id,
                "comment_kind": "LINE",
                "start_line": "9",
                "end_line": "9",
                "start_column": "1",
                "end_column": "20",
                "raw_text": "// load orders",
                "normalized_text": "load orders",
                "language": "csharp",
                "encoding": "utf-8",
                "properties_json": "{}",
            }
            issue_id = f"issue:{suffix}"
            graph.issues[issue_id] = {
                "issue_id": issue_id,
                "issue_type": "DYNAMIC_SQL",
                "severity": "ERROR" if suffix == "b" else "WARNING",
                "source_node_id": owner_id,
                "raw_reference": "SELECT * FROM ORDERS",
                "database_key": "DB",
                "source_path": "api/Owner.cs",
                "start_line": "10",
                "message": "Duplicate SQL diagnostic",
                "properties_json": json.dumps({suffix: True}),
            }

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="system",
                config_path="extractor.json",
                output_mode="partitioned",
            )

            sql_nodes = _db_rows(
                output, "nodes", "qualified_name = ?", ("Api.Owner.inline-sql.10",)
            )
            self.assertEqual(len(sql_nodes), 1)
            self.assertEqual(sql_nodes[0]["stable_id"], duplicate_a)
            self.assertEqual(sql_nodes[0]["graph_role"], "MAIN")
            self.assertEqual(
                json.loads(sql_nodes[0]["properties_json"]),
                {
                    "dynamic": False,
                    "source": "declaration",
                    "sql": "SELECT * FROM ORDERS",
                },
            )
            self.assertEqual(
                len(
                    _db_rows(
                        output,
                        "edges",
                        "target_node_id = ? AND edge_type = 'CONTAINS'",
                        (sql_nodes[0]["node_id"],),
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    _db_rows(
                        output,
                        "evidence",
                        "target_id = ?",
                        (sql_nodes[0]["node_id"],),
                    )
                ),
                1,
            )
            self.assertEqual(
                len(
                    _db_rows(
                        output,
                        "comments",
                        "owner_node_id = ?",
                        (sql_nodes[0]["node_id"],),
                    )
                ),
                1,
            )
            issues = _db_rows(
                output, "issues", "source_node_id = ?", (sql_nodes[0]["node_id"],)
            )
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["severity"], "ERROR")
            self.assertEqual(
                json.loads(issues[0]["properties_json"]), {"a": True, "b": True}
            )

            reference = _db_rows(
                output, "nodes", "stable_id = ?", (reference_holder,)
            )[0]
            self.assertEqual(
                json.loads(reference["properties_json"])["referenced_node_id"],
                sql_nodes[0]["node_id"],
            )
            self.assertEqual(
                len(_db_rows(output, "nodes", "node_type = 'LOCAL_ROUTINE'")), 2
            )
            self.assertEqual(
                len(_db_rows(output, "evidence", "target_type = 'EDGE'")), 1
            )

    def test_knowledge_markdown_chunks_and_indexes_each_section(self) -> None:
        graph = GraphPackage()
        graph.register_source("api", "dotnet-api", "api", "api")
        for number in range(12):
            node_id = f"api-operation:api:GET:/orders/{number}"
            graph.nodes[node_id] = _node(
                node_id,
                "API_OPERATION",
                f"GET /orders/{number}",
                "api",
                {"method": "GET", "route": f"/orders/{number}"},
            )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="system",
                config_path="extractor.json",
                output_mode="partitioned",
                knowledge_chunking={
                    "enabled": True,
                    "maxMarkdownBytes": 1024,
                },
            )
            manifest = _json(output / "knowledge/manifest.json")
            api_files = [
                item for item in manifest["files"] if item["topic"] == "APIs"
            ]
            self.assertGreater(len(api_files), 1)
            index = _json(output / "graph-index.json")
            self.assertEqual(len(index["knowledgeByNodeId"]), 12)
            self.assertTrue(
                all(len(values) == 1 for values in index["knowledgeByNodeId"].values())
            )

    def test_partition_keeps_failed_empty_source_package(self) -> None:
        graph = GraphPackage()
        graph.register_source("broken", "angular", "broken", "broken")
        graph.add_issue(
            "PARSE_ERROR",
            "Parser failed before producing nodes",
            properties={"source_key": "broken"},
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="system",
                config_path="extractor.json",
                output_mode="partitioned",
            )
            manifest = _json(output / "manifest.json")
            self.assertIn("broken", manifest["packages"])
            issues = _db_rows(
                output, "issues", "package_key = ?", ("sources/broken",)
            )
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0]["issue_type"], "PARSE_ERROR")

    def test_partitioned_output_memory_knowledge_and_queries(self) -> None:
        graph = _sample_graph()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="order-system",
                config_path="extractor.json",
                output_mode="partitioned",
                max_evidence_snippet_chars=20,
                max_issues_per_type_per_file=2,
            )

            manifest = _json(output / "manifest.json")
            self.assertEqual(manifest["contractVersion"], "2.0")
            self.assertEqual(manifest["storage"], "sqlite")
            self.assertEqual(manifest["outputMode"], "partitioned")
            self.assertEqual(
                sorted(manifest["packages"]), ["api", "db", "global", "ui"]
            )
            self.assertTrue((output / "graph.sqlite").is_file())
            self.assertFalse((output / "sources/api/nodes.csv").exists())

            global_edges = _db_rows(
                output, "edges", "package_key = ?", ("global",)
            )
            self.assertEqual(
                {row["edge_type"] for row in global_edges},
                {"CALLS_API", "READS_FROM"},
            )
            api_nodes = _db_rows(
                output, "nodes", "package_key = ?", ("sources/api",)
            )
            self.assertIn(
                "api-operation:api:GET:/orders/{id}",
                {row["stable_id"] for row in api_nodes},
            )
            self.assertTrue(all(row["node_id"].isdigit() for row in api_nodes))

            index = _json(output / "graph-index.json")
            self.assertEqual(index["indexVersion"], "2.0")
            self.assertIn("GET /orders/{id}", index["apisByMethodPath"])
            self.assertIn("DB.APP.ORDERS", index["tablesByQName"])
            self.assertEqual(len(index["memoryByNodeId"]), 2)

            memory = _json(output / "codebase-memory/manifest.json")
            self.assertGreaterEqual(memory["statistics"]["relationships"], 2)
            api_cards = [
                json.loads(line)
                for line in (
                    output / "codebase-memory/entities/apis.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(api_cards), 1)
            self.assertTrue(api_cards[0]["evidence_ids"])
            self.assertTrue(api_cards[0]["related_node_ids"])
            self.assertTrue(api_cards[0]["knowledge_refs"])
            self.assertEqual(len(api_cards[0]["content_hash"]), 64)

            knowledge = _json(output / "knowledge/manifest.json")
            topics = {item["topic"] for item in knowledge["files"]}
            self.assertEqual(
                topics, {"APIs", "Flows", "Databases", "Jobs", "CrossSystem"}
            )
            api_markdown = (output / "knowledge/APIs.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("node_id:", api_markdown)
            self.assertIn("evidence_ids:", api_markdown)

            evidence = _db_rows(
                output, "evidence", "package_key = ?", ("sources/api",)
            )
            self.assertLessEqual(len(evidence[0]["snippet"]), 20)
            issues = _db_rows(
                output, "issues", "package_key = ?", ("sources/ui",)
            )
            self.assertEqual(len(issues), 2)
            self.assertTrue(
                any("additional" in issue["message"] for issue in issues)
            )

            store = KnowledgeStore(output)
            impact = store.impact_api("GET", "/orders/42")
            self.assertEqual(
                impact["data"]["target_stable_ids"], ["table:DB:APP.ORDERS"]
            )
            table_impact = store.impact_table("DB", "APP", "ORDERS")
            self.assertIn(
                "api-operation:api:GET:/orders/{id}",
                table_impact["data"]["target_stable_ids"],
            )
            trace = store.trace_ui_to_db("GET /orders/{id}")
            self.assertIn("table:DB:APP.ORDERS", trace["data"]["target_stable_ids"])
            self.assertGreaterEqual(
                len(store.search_memory("orders")["data"]["memory"]), 1
            )
            opened = store.open_source("evidence:api")
            self.assertEqual(
                opened["source_locations"][0]["source_path"],
                "api/OrdersController.cs",
            )
            self.assertEqual(len(store.list_issues(source="ui")["issues"]), 2)
            explained = store.explain_node(
                "api-operation:api:GET:/orders/{id}"
            )
            self.assertIsNotNone(explained["data"]["memory"])

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = query_main(
                    [
                        "--output",
                        str(output),
                        "impact-api",
                        "--method",
                        "GET",
                        "--path",
                        "/orders/42",
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn(
                "table:DB:APP.ORDERS",
                json.loads(stdout.getvalue())["data"]["target_stable_ids"],
            )

    def test_flat_mode_uses_the_same_sqlite_contract(self) -> None:
        graph = _sample_graph()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="order-system",
                config_path="extractor.json",
                output_mode="flat",
            )
            manifest = _json(output / "manifest.json")
            self.assertEqual(manifest["outputMode"], "flat")
            self.assertEqual(manifest["storage"], "sqlite")
            self.assertTrue((output / "graph.sqlite").is_file())
            self.assertTrue((output / "graph-index.json").is_file())
            store = KnowledgeStore(output)
            found = store.find_node(
                node_id="api-operation:api:GET:/orders/{id}"
            )
            self.assertEqual(
                found["stable_node_ids"],
                ["api-operation:api:GET:/orders/{id}"],
            )
            self.assertTrue(found["node_ids"][0].isdigit())


def _sample_graph() -> GraphPackage:
    graph = GraphPackage()
    for source, source_type, relative_path in (
        ("ui", "angular", "ui/order.service.ts"),
        ("api", "dotnet-api", "api/OrdersController.cs"),
        ("db", "oracle-plsql", "db/orders.sql"),
    ):
        graph.add_source(
            SourceRecord(
                source_key=source,
                source_type=source_type,
                system_key=source,
                repository_key=source,
                relative_path=relative_path,
                declared_encoding="utf-8",
                actual_encoding="utf-8",
                raw_sha256="raw",
                text_sha256="text",
                newline_style="LF",
                bom="",
            )
        )

    api_call = "api-call:ui:GET:/orders/{id}"
    api_operation = "api-operation:api:GET:/orders/{id}"
    table = "table:DB:APP.ORDERS"
    graph.nodes[api_call] = _node(
        api_call,
        "API_CALL_REFERENCE",
        "GET /orders/{id}",
        "ui",
        {"method": "GET", "route": "/orders/{id}"},
    )
    graph.nodes[api_operation] = _node(
        api_operation,
        "API_OPERATION",
        "GET /orders/{id}",
        "api",
        {"method": "GET", "route": "/orders/{id}"},
    )
    graph.nodes[table] = _node(
        table,
        "TABLE",
        "DB.APP.ORDERS",
        "db",
        {"database": "DB", "schema": "APP", "table": "ORDERS"},
        database="DB",
    )
    api_edge = _edge(graph, api_call, "CALLS_API", api_operation)
    db_edge = _edge(graph, api_operation, "READS_FROM", table)

    graph.evidence["evidence:api"] = {
        "evidence_id": "evidence:api",
        "target_type": "NODE",
        "target_id": api_operation,
        "source_path": "api/OrdersController.cs",
        "start_line": "10",
        "end_line": "12",
        "start_column": "1",
        "end_column": "20",
        "evidence_kind": "DECLARATION",
        "extractor_name": "test",
        "confidence": "1.0",
        "snippet": "This snippet is deliberately longer than twenty characters.",
        "properties_json": "{}",
    }
    graph.evidence["evidence:edge"] = {
        "evidence_id": "evidence:edge",
        "target_type": "EDGE",
        "target_id": db_edge,
        "source_path": "api/OrdersController.cs",
        "start_line": "20",
        "end_line": "20",
        "start_column": "1",
        "end_column": "20",
        "evidence_kind": "REFERENCE",
        "extractor_name": "test",
        "confidence": "0.8",
        "snippet": "SELECT * FROM APP.ORDERS",
        "properties_json": "{}",
    }
    for number in range(3):
        graph.add_issue(
            "DYNAMIC_CONFIG_KEY",
            f"Dynamic configuration {number}",
            source_path="ui/order.service.ts",
            severity="WARNING",
        )
    return graph


def _node(
    node_id: str,
    node_type: str,
    qualified_name: str,
    repository: str,
    properties: dict[str, object],
    *,
    database: str = "",
) -> dict[str, str]:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "technical_name": qualified_name.rsplit(".", 1)[-1],
        "qualified_name": qualified_name,
        "default_display_name": qualified_name,
        "system_key": repository,
        "database_key": database,
        "repository_key": repository,
        "graph_role": "MAIN",
        "confidence": "1.0",
        "properties_json": json.dumps(properties),
    }


def _edge(
    graph: GraphPackage, source: str, edge_type: str, target: str
) -> str:
    edge_id = canonical_edge_id(source, edge_type, target, "", "DATA_FLOW")
    graph.edges[edge_id] = {
        "edge_id": edge_id,
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
        "graph_layer": "DATA_FLOW",
        "raw_operation": "",
        "confidence": "0.8",
        "properties_json": "{}",
    }
    return edge_id


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _db_rows(
    output: Path,
    table: str,
    where: str = "",
    parameters: tuple[object, ...] = (),
) -> list[dict[str, str]]:
    connection = sqlite3.connect(output / "graph.sqlite")
    try:
        connection.row_factory = sqlite3.Row
        suffix = f" WHERE {where}" if where else ""
        return [
            {
                key: "" if row[key] is None else str(row[key])
                for key in row.keys()
            }
            for row in connection.execute(
                f"SELECT * FROM {table}{suffix}", parameters
            )
        ]
    finally:
        connection.close()


if __name__ == "__main__":
    unittest.main()
