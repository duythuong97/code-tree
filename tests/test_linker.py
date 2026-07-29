from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from code_tree_exporter.contract.graph_contract import canonical_edge_id
from code_tree_exporter.graph_package import GraphPackage
from code_tree_exporter.linker import run_linker


class GlobalLinkerTests(unittest.TestCase):
    def test_cross_source_references_resolve_independent_of_merge_order(self) -> None:
        results = []
        for targets_first in (False, True):
            graph = _cross_source_graph(targets_first)
            run_linker(graph)
            results.append(
                {
                    (
                        edge["source_node_id"],
                        edge["edge_type"],
                        edge["target_node_id"],
                    )
                    for edge in graph.edges.values()
                }
            )
            self.assertFalse(
                any(
                    node["node_type"] == "UNRESOLVED_REFERENCE"
                    for node in graph.nodes.values()
                )
            )
            self.assertTrue(
                all(
                    edge["source_node_id"] in graph.nodes
                    and edge["target_node_id"] in graph.nodes
                    for edge in graph.edges.values()
                )
            )
            self.assertFalse(
                {"TABLE_NOT_IMPORTED", "COLUMN_NOT_IMPORTED"}
                & {issue["issue_type"] for issue in graph.issues.values()}
            )
            holder = graph.nodes["method:api:holder"]
            references = json.loads(holder["properties_json"])["references"]
            self.assertEqual(
                references,
                ["table:DB:ORDERS", "procedure:DB:PKG:LOAD:void"],
            )
            self.assertTrue(
                all(
                    evidence["target_id"] in graph.edges
                    for evidence in graph.evidence.values()
                    if evidence["target_type"] == "EDGE"
                )
            )
        self.assertEqual(results[0], results[1])

    def test_ambiguous_routine_overload_is_not_guessed(self) -> None:
        graph = GraphPackage()
        caller = "method:api:ambiguous"
        unresolved = "unresolved-reference:DB:ROUTINE:PKG:LOAD"
        graph.nodes[caller] = _node(caller, "METHOD", "Api.Ambiguous", "api")
        graph.nodes[unresolved] = _node(
            unresolved,
            "UNRESOLVED_REFERENCE",
            "DB.PKG.LOAD",
            "api",
            database="DB",
            properties={"database": "DB", "package": "PKG", "routine": "LOAD"},
        )
        for signature in ("NUMBER", "VARCHAR2"):
            routine = f"procedure:DB:PKG:LOAD:{signature}"
            graph.nodes[routine] = _node(
                routine,
                "PROCEDURE",
                f"DB.PKG.LOAD({signature})",
                "db",
                database="DB",
                properties={
                    "database": "DB",
                    "package": "PKG",
                    "routine": "LOAD",
                    "signature": signature,
                },
            )
        _edge(graph, caller, "CALLS", unresolved)

        run_linker(graph)

        self.assertIn(unresolved, graph.nodes)
        self.assertEqual(next(iter(graph.edges.values()))["target_node_id"], unresolved)
        self.assertIn(
            "AMBIGUOUS_SYMBOL",
            {issue["issue_type"] for issue in graph.issues.values()},
        )

    def test_large_reference_set_resolves_with_indexed_pass(self) -> None:
        graph = GraphPackage()
        caller = "method:api:bulk"
        graph.nodes[caller] = _node(caller, "METHOD", "Api.Bulk", "api")
        count = 2_000
        for number in range(count):
            table = f"TABLE_{number}"
            table_node = f"table:DB:{table}"
            unresolved = f"unresolved-reference:DB:TABLE:APP:{table}"
            graph.nodes[table_node] = _node(
                table_node,
                "TABLE",
                f"DB.APP.{table}",
                "db",
                database="DB",
                properties={"database": "DB", "schema": "APP", "table": table},
            )
            graph.nodes[unresolved] = _node(
                unresolved,
                "UNRESOLVED_REFERENCE",
                f"DB.APP.{table}",
                "api",
                database="DB",
                properties={"database": "DB", "schema": "APP", "table": table},
            )
            _edge(graph, caller, "READS_FROM", unresolved)

        run_linker(graph)

        self.assertEqual(len(graph.edges), count)
        self.assertEqual(
            sum(
                node["node_type"] == "UNRESOLVED_REFERENCE"
                for node in graph.nodes.values()
            ),
            0,
        )
        self.assertTrue(
            all(
                edge["target_node_id"].startswith("table:DB:TABLE_")
                for edge in graph.edges.values()
            )
        )

    def test_missing_endpoint_becomes_publishable_unresolved_node(self) -> None:
        graph = GraphPackage()
        caller = "method:api:missing"
        graph.nodes[caller] = _node(
            caller,
            "METHOD",
            "Api.Missing",
            "api",
            properties={
                "semantic_tree": {"ref_node_id": "table:DB:PROPERTY_ONLY"}
            },
        )
        _edge(graph, caller, "READS_FROM", "table:DB:NOT_EXTRACTED")

        run_linker(graph)

        unresolved = [
            node_id
            for node_id, node in graph.nodes.items()
            if node["node_type"] == "UNRESOLVED_REFERENCE"
        ]
        self.assertEqual(len(unresolved), 2)
        self.assertIn(next(iter(graph.edges.values()))["target_node_id"], unresolved)
        property_reference = json.loads(graph.nodes[caller]["properties_json"])[
            "semantic_tree"
        ]["ref_node_id"]
        self.assertIn(property_reference, unresolved)
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            graph.write(
                output,
                source_name="system",
                config_path="extractor.json",
                output_mode="partitioned",
            )
            connection = sqlite3.connect(output / "graph.sqlite")
            try:
                self.assertEqual(list(connection.execute("PRAGMA foreign_key_check")), [])
            finally:
                connection.close()


def _cross_source_graph(targets_first: bool) -> GraphPackage:
    graph = GraphPackage()
    caller = "method:api:load"
    holder = "method:api:holder"
    table = "table:DB:ORDERS"
    column = "column:DB:ORDERS:ID"
    routine = "procedure:DB:PKG:LOAD:void"
    unresolved_table = "unresolved-reference:DB:TABLE:APP:ORDERS"
    unresolved_column = "unresolved-reference:DB:COLUMN:APP:ORDERS:ID"
    unresolved_routine = "unresolved-reference:DB:ROUTINE:PKG:LOAD"

    targets = {
        table: _node(
            table,
            "TABLE",
            "DB.APP.ORDERS",
            "db",
            database="DB",
            properties={"database": "DB", "schema": "APP", "table": "ORDERS"},
        ),
        column: _node(
            column,
            "COLUMN",
            "DB.APP.ORDERS.ID",
            "db",
            database="DB",
            properties={
                "database": "DB",
                "schema": "APP",
                "table": "ORDERS",
                "column": "ID",
            },
        ),
        routine: _node(
            routine,
            "PROCEDURE",
            "DB.PKG.LOAD()",
            "db",
            database="DB",
            properties={"database": "DB", "package": "PKG", "routine": "LOAD"},
        ),
    }
    references = {
        caller: _node(caller, "METHOD", "Api.Load", "api"),
        holder: _node(
            holder,
            "METHOD",
            "Api.Holder",
            "api",
            properties={"references": [unresolved_table, unresolved_routine]},
        ),
        unresolved_table: _node(
            unresolved_table,
            "UNRESOLVED_REFERENCE",
            "DB.APP.ORDERS",
            "api",
            database="DB",
            properties={"database": "DB", "schema": "APP", "table": "ORDERS"},
        ),
        unresolved_column: _node(
            unresolved_column,
            "UNRESOLVED_REFERENCE",
            "DB.APP.ORDERS.ID",
            "api",
            database="DB",
            properties={
                "database": "DB",
                "schema": "APP",
                "table": "ORDERS",
                "column": "ID",
            },
        ),
        unresolved_routine: _node(
            unresolved_routine,
            "UNRESOLVED_REFERENCE",
            "DB.PKG.LOAD",
            "api",
            database="DB",
            properties={"database": "DB", "package": "PKG", "routine": "LOAD"},
        ),
    }
    for collection in ((targets, references) if targets_first else (references, targets)):
        graph.nodes.update(collection)

    for number, (edge_type, target) in enumerate(
        (
            ("READS_FROM", unresolved_table),
            ("MAPS_TO", unresolved_column),
            ("CALLS", unresolved_routine),
        )
    ):
        edge_id = _edge(graph, caller, edge_type, target)
        evidence_id = f"evidence:{number}"
        graph.evidence[evidence_id] = {
            "evidence_id": evidence_id,
            "target_type": "EDGE",
            "target_id": edge_id,
            "source_path": "api/Load.cs",
            "start_line": str(number + 1),
            "end_line": str(number + 1),
            "start_column": "1",
            "end_column": "20",
            "evidence_kind": "REFERENCE",
            "extractor_name": "test",
            "confidence": "0.5",
            "snippet": "reference",
            "properties_json": "{}",
        }
    for issue_id, issue_type, raw_reference in (
        ("issue:table", "TABLE_NOT_IMPORTED", "APP.ORDERS"),
        ("issue:column", "COLUMN_NOT_IMPORTED", "ORDERS.ID"),
    ):
        graph.issues[issue_id] = {
            "issue_id": issue_id,
            "issue_type": issue_type,
            "severity": "ERROR",
            "source_node_id": caller,
            "raw_reference": raw_reference,
            "database_key": "DB",
            "source_path": "api/Load.cs",
            "start_line": "1",
            "message": "Missing catalog object",
            "properties_json": "{}",
        }
    return graph


def _node(
    node_id: str,
    node_type: str,
    qualified_name: str,
    repository: str,
    *,
    database: str = "",
    properties: dict[str, object] | None = None,
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
        "properties_json": json.dumps(properties or {}),
    }


def _edge(graph: GraphPackage, source: str, edge_type: str, target: str) -> str:
    edge_id = canonical_edge_id(source, edge_type, target, "", "TECHNICAL")
    graph.edges[edge_id] = {
        "edge_id": edge_id,
        "source_node_id": source,
        "target_node_id": target,
        "edge_type": edge_type,
        "graph_layer": "TECHNICAL",
        "raw_operation": "",
        "confidence": "0.5",
        "properties_json": "{}",
    }
    return edge_id


if __name__ == "__main__":
    unittest.main()
