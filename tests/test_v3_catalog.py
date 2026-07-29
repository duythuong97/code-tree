from __future__ import annotations

import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from code_tree_exporter.cli import main as cli_main
from code_tree_exporter.contract.graph_contract import (
    api_operation_id,
    canonical_edge_id,
    stable_node_id,
    table_id,
)
from code_tree_exporter.extractors.package_support.package_writer import Catalog
from code_tree_exporter.graph_package import GraphPackage
from code_tree_exporter.markdown_export import export_markdown
from code_tree_exporter.pipeline import run_pipeline
from code_tree_exporter.query import KnowledgeStore
from code_tree_exporter.v3.catalog import prepare_catalog
from code_tree_exporter.v3.hierarchy import enrich_hierarchy
from code_tree_exporter.v3.publisher import publish_v3


class V3CatalogTest(unittest.TestCase):
    def test_hierarchy_links_system_application_project_and_database_module(self) -> None:
        graph = GraphPackage()
        app_id = stable_node_id("api-application", "ORDER_API")
        solution_id = stable_node_id("dotnet-solution", "order-api", "Order.sln")
        project_id = stable_node_id("dotnet-project", "order-api", "Order.csproj")
        database_stable = stable_node_id("database", "DB1")
        package_id = stable_node_id("plsql-package", "DB1", "ORDER_PKG")
        graph.nodes[app_id] = _node(
            app_id, "API_APPLICATION", "ORDER_API", "ORDER_API"
        )
        graph.nodes[solution_id] = _node(
            solution_id, "DOTNET_SOLUTION", "Order.sln", "order-api/Order.sln"
        )
        graph.nodes[project_id] = _node(
            project_id, "DOTNET_PROJECT", "Order.csproj", "order-api/Order.csproj"
        )
        graph.nodes[database_stable] = _node(
            database_stable, "DATABASE", "DB1", "DB1", database="DB1"
        )
        graph.nodes[package_id] = _node(
            package_id,
            "PLSQL_PACKAGE",
            "ORDER_PKG",
            "DB1.ORDER_PKG",
            database="DB1",
        )
        project_edge = canonical_edge_id(
            solution_id, "CONTAINS", project_id, "", "STRUCTURAL"
        )
        graph.edges[project_edge] = _edge(
            project_edge,
            solution_id,
            project_id,
            "CONTAINS",
            "",
            graph_layer="STRUCTURAL",
        )

        enrich_hierarchy(graph)

        structural = {
            (edge["source_node_id"], edge["target_node_id"])
            for edge in graph.edges.values()
            if edge["edge_type"] == "CONTAINS"
        }
        system_id = stable_node_id("system", "ORDER_SYSTEM")
        self.assertIn((system_id, app_id), structural)
        self.assertIn((app_id, solution_id), structural)
        self.assertIn((solution_id, project_id), structural)
        self.assertIn((system_id, database_stable), structural)
        self.assertIn((database_stable, package_id), structural)

    def test_custom_profile_normalizes_arbitrary_csv_for_existing_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "catalog" / "incoming"
            profiles = root / "catalog" / "profiles"
            incoming.mkdir(parents=True)
            profiles.mkdir()
            _write_csv(
                incoming / "scheduler-export.csv",
                ["network", "step", "program", "previous"],
                [["DAILY", "LOAD_ORDERS", "OrderBatch.exe", ""]],
            )
            (profiles / "jobnet.json").write_text(
                json.dumps(
                    {
                        "name": "scheduler-jobnet",
                        "catalogType": "batch-jobs",
                        "match": {
                            "filename": "scheduler-*.csv",
                            "requiredHeaders": [
                                "network",
                                "step",
                                "program",
                                "previous",
                            ],
                        },
                        "fields": {
                            "jobnet_id": "network",
                            "job_id": "step",
                            "executable_name": "program",
                            "predecessor_job_id": "previous",
                        },
                        "transforms": {
                            "jobnet_id": ["trim", "upper"],
                            "job_id": ["trim", "upper"],
                        },
                        "output": {
                            "filename": "jobnet.csv",
                            "fields": [
                                "jobnet_id",
                                "job_id",
                                "predecessor_job_id",
                                "executable_name",
                            ],
                            "identity": ["jobnet_id", "job_id"],
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = prepare_catalog(
                {"catalog": {"folder": str(root / "catalog")}},
                config_dir=root,
                staging_root=root / "staging",
            )
            assert result is not None
            with (result.normalized_root / "jobnet.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(
                [
                    {
                        "jobnet_id": "DAILY",
                        "job_id": "LOAD_ORDERS",
                        "predecessor_job_id": "",
                        "executable_name": "OrderBatch.exe",
                    }
                ],
                rows,
            )
            self.assertEqual("batch-jobs", result.files[0].catalog_type)

    def test_legacy_cli_source_type_listing_still_works(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["--list-source-types"])
        self.assertEqual(0, exit_code)
        self.assertIn("sql-files", output.getvalue())

    def test_pipeline_auto_imports_catalog_before_existing_sql_extractor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            sql_root = source_root / "sql"
            sql_root.mkdir(parents=True)
            (sql_root / "orders.sql").write_text(
                "SELECT ORDER_ID FROM ORDERS;\n", encoding="utf-8"
            )
            incoming = root / "catalog" / "incoming"
            incoming.mkdir(parents=True)
            for database, schema in (("DB1", "APP"), ("DB2", "MASTER")):
                _write_csv(
                    incoming / f"database-tables__{database}.csv",
                    ["database_key", "schema_name", "object_name", "object_type"],
                    [[database, schema, "ORDERS", "TABLE"]],
                )
                _write_csv(
                    incoming / f"database-columns__{database}.csv",
                    [
                        "database_key",
                        "schema_name",
                        "object_name",
                        "column_name",
                        "data_type",
                    ],
                    [[database, schema, "ORDERS", "ORDER_ID", "NUMBER"]],
                )
            config_path = root / "config.json"
            output = root / "output"
            config_path.write_text(
                json.dumps(
                    {
                        "version": "3",
                        "name": "ORDER_SYSTEM",
                        "root": str(source_root),
                        "output": str(output),
                        "catalog": {"folder": str(root / "catalog")},
                        "sources": [
                            {
                                "name": "order-sql",
                                "type": "sql-files",
                                "folders": ["sql"],
                                "database": "DB1",
                                "schema": "APP",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            run_pipeline(config_path)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("3.0", manifest["contractVersion"])
            self.assertEqual(4, manifest["statistics"]["catalogFiles"])
            store = KnowledgeStore(output)
            health = store.health()
            self.assertGreater(health["data"]["metrics"]["nodes"], 0)
            catalog_status = store.catalog_status()
            self.assertEqual(4, len(catalog_status["data"]["catalog_files"]))
            export_markdown(output)
            self.assertTrue((output / "markdown-manifest.json").is_file())
            self.assertTrue((output / "knowledge" / "manifest.json").is_file())
            self.assertTrue((output / "quality-report.json").is_file())

    def test_four_database_files_compile_to_legacy_catalog_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "catalog" / "incoming"
            incoming.mkdir(parents=True)
            _write_csv(
                incoming / "database-tables__DB1.csv",
                ["database_key", "schema_name", "object_name", "object_type"],
                [["DB1", "APP", "ORDERS", "TABLE"]],
            )
            _write_csv(
                incoming / "database-columns__DB1.csv",
                [
                    "database_key",
                    "schema_name",
                    "object_name",
                    "column_name",
                    "data_type",
                ],
                [["DB1", "APP", "ORDERS", "ORDER_ID", "NUMBER"]],
            )
            _write_csv(
                incoming / "database-tables__DB2.csv",
                ["database_key", "schema_name", "object_name", "object_type"],
                [["DB2", "MASTER", "ORDERS", "TABLE"]],
            )
            _write_csv(
                incoming / "database-columns__DB2.csv",
                [
                    "database_key",
                    "schema_name",
                    "object_name",
                    "column_name",
                    "data_type",
                ],
                [["DB2", "MASTER", "ORDERS", "ORDER_ID", "NUMBER"]],
            )

            result = prepare_catalog(
                {"catalog": {"folder": str(root / "catalog")}},
                config_dir=root,
                staging_root=root / "staging",
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(2, len(result.tables))
            self.assertEqual(2, len(result.columns))
            self.assertEqual(4, len(result.files))

            with (result.normalized_root / "tables.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(2, len(rows))
            self.assertNotEqual(rows[0]["columns_file"], rows[1]["columns_file"])

            catalog = Catalog.load(result.normalized_root)
            self.assertTrue(catalog.has_table("DB1", "ORDERS"))
            self.assertTrue(catalog.has_column("DB2", "ORDERS", "ORDER_ID"))

            graph = GraphPackage()
            result.merge_into(graph, system_key="ORDER_SYSTEM")
            self.assertTrue(
                any(node["node_type"] == "DATABASE_SCHEMA" for node in graph.nodes.values())
            )
            self.assertIn(table_id("DB1", "ORDERS"), graph.nodes)
            self.assertIn(table_id("DB2", "ORDERS"), graph.nodes)

    def test_v3_publisher_adds_io_flow_and_quality_without_changing_graph_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            graph = GraphPackage()
            api_id = api_operation_id("ORDER_API", "POST", "/orders/{id}")
            table_stable = table_id("DB1", "ORDERS")
            graph.nodes[api_id] = _node(
                api_id,
                "API_OPERATION",
                "POST /orders/{id}",
                "POST /orders/{id}",
                properties={
                    "method": "POST",
                    "route": "/orders/{id}",
                    "semantic_tree": {
                        "parameters": [
                            {"name": "request", "type": "OrderRequest"}
                        ]
                    },
                },
            )
            graph.nodes[table_stable] = _node(
                table_stable,
                "TABLE",
                "ORDERS",
                "DB1.APP.ORDERS",
                database="DB1",
                properties={"database": "DB1", "schema": "APP", "table": "ORDERS"},
            )
            edge_id = canonical_edge_id(
                api_id, "INSERTS", table_stable, "INSERT", "DATA_FLOW"
            )
            graph.edges[edge_id] = {
                "edge_id": edge_id,
                "source_node_id": api_id,
                "target_node_id": table_stable,
                "edge_type": "INSERTS",
                "graph_layer": "DATA_FLOW",
                "raw_operation": "INSERT",
                "confidence": "1.0",
                "properties_json": "{}",
            }
            graph.write(
                output,
                source_name="ORDER_SYSTEM",
                config_path=str(root / "config.json"),
                output_mode="flat",
                knowledge_chunking={},
                max_evidence_snippet_chars=500,
                max_issues_per_type_per_file=20,
            )
            with sqlite3.connect(output / "graph.sqlite") as connection:
                node_ids_before = dict(
                    connection.execute("SELECT stable_id, node_id FROM nodes")
                )
                edge_ids_before = dict(
                    connection.execute("SELECT stable_id, edge_id FROM edges")
                )
            publish_v3(output, catalog=None, config={})

            with sqlite3.connect(output / "graph.sqlite") as connection:
                self.assertEqual(3, connection.execute("PRAGMA user_version").fetchone()[0])
                self.assertGreater(
                    connection.execute("SELECT COUNT(*) FROM io_items").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    1, connection.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
                )
                stable_ids = {
                    row[0] for row in connection.execute("SELECT stable_id FROM nodes")
                }
                self.assertEqual(
                    node_ids_before,
                    dict(connection.execute("SELECT stable_id, node_id FROM nodes")),
                )
                self.assertEqual(
                    edge_ids_before,
                    dict(connection.execute("SELECT stable_id, edge_id FROM edges")),
                )
            self.assertIn(api_id, stable_ids)
            self.assertIn(table_stable, stable_ids)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("3.0", manifest["contractVersion"])
            self.assertEqual("quality-report.json", manifest["files"]["qualityReport"])
            self.assertIn("quality-report.json", manifest["checksums"])
            self.assertIn("QUALITY_REPORT.md", manifest["checksums"])

    def test_flow_materialization_handles_one_thousand_batch_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "out"
            graph = GraphPackage()
            executable_id = stable_node_id("executable", "batch-system", "ORDER.EXE")
            table_stable = table_id("DB1", "ORDERS")
            graph.nodes[executable_id] = _node(
                executable_id,
                "EXECUTABLE",
                "ORDER.EXE",
                "batch-system.ORDER.EXE",
            )
            graph.nodes[table_stable] = _node(
                table_stable,
                "TABLE",
                "ORDERS",
                "DB1.APP.ORDERS",
                database="DB1",
            )
            write_edge = canonical_edge_id(
                executable_id, "INSERTS", table_stable, "INSERT", "DATA_FLOW"
            )
            graph.edges[write_edge] = _edge(
                write_edge, executable_id, table_stable, "INSERTS", "INSERT"
            )
            for index in range(1_000):
                job_id = stable_node_id(
                    "job", "batch-system", "DAILY", f"JOB_{index:04d}"
                )
                graph.nodes[job_id] = _node(
                    job_id,
                    "JOB",
                    f"JOB_{index:04d}",
                    f"DAILY.JOB_{index:04d}",
                )
                edge_id = canonical_edge_id(
                    job_id, "STARTS", executable_id, "", "TECHNICAL"
                )
                graph.edges[edge_id] = _edge(
                    edge_id, job_id, executable_id, "STARTS", ""
                )
            graph.write(
                output,
                source_name="ORDER_SYSTEM",
                config_path=str(root / "config.json"),
                output_mode="flat",
                knowledge_chunking={},
                max_evidence_snippet_chars=500,
                max_issues_per_type_per_file=20,
            )
            publish_v3(output, catalog=None, config={})
            with sqlite3.connect(output / "graph.sqlite") as connection:
                self.assertEqual(
                    1_000,
                    connection.execute("SELECT COUNT(*) FROM flows").fetchone()[0],
                )


def _write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _node(
    node_id: str,
    node_type: str,
    name: str,
    qualified_name: str,
    *,
    database: str = "",
    properties: dict[str, object] | None = None,
) -> dict[str, str]:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "technical_name": name,
        "qualified_name": qualified_name,
        "default_display_name": name,
        "system_key": "ORDER_SYSTEM",
        "database_key": database,
        "repository_key": "test",
        "graph_role": "MAIN",
        "confidence": "1.0",
        "properties_json": json.dumps(properties or {}, separators=(",", ":")),
    }


def _edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    edge_type: str,
    raw_operation: str,
    *,
    graph_layer: str | None = None,
) -> dict[str, str]:
    return {
        "edge_id": edge_id,
        "source_node_id": source_id,
        "target_node_id": target_id,
        "edge_type": edge_type,
        "graph_layer": graph_layer or ("DATA_FLOW" if raw_operation else "TECHNICAL"),
        "raw_operation": raw_operation,
        "confidence": "1.0",
        "properties_json": "{}",
    }


if __name__ == "__main__":
    unittest.main()
