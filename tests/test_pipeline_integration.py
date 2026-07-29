from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from code_tree_exporter.pipeline import run_pipeline
from code_tree_exporter.query import KnowledgeStore


class PipelineIntegrationTests(unittest.TestCase):
    def test_angular_keeps_literal_route_component_and_http_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            source_root.mkdir()
            (source_root / "app.ts").write_text(
                "import { Http } from '@angular/http';\n"
                "@Component({ selector: 'order-page' })\n"
                "export class OrderComponent {\n"
                "  load() { return this.http.get('/api/orders/42'); }\n"
                "}\n"
                "const routes = [\n"
                "  { path: 'orders', component: OrderComponent }\n"
                "];\n",
                encoding="utf-8",
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "sources": [
                            {
                                "name": "web",
                                "type": "angular",
                                "folders": ["."],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            index = json.loads(
                (output / "graph-index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["nodeTypeCounts"]["ANGULAR_COMPONENT"], 1)
            self.assertEqual(index["nodeTypeCounts"]["API_CALL_REFERENCE"], 1)
            self.assertEqual(index["nodeTypeCounts"]["SCREEN"], 1)

    @unittest.skipUnless(shutil.which("dotnet"), ".NET SDK is not installed")
    def test_dotnet_route_normalization_and_handler_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            source_root.mkdir()
            (source_root / "OrdersController.cs").write_text(
                "using Microsoft.AspNetCore.Mvc;\n"
                "[ApiController]\n"
                "[Route(\"api/[controller]\")]\n"
                "public class OrdersController : ControllerBase {\n"
                "  [HttpGet(\"{id:int}\")]\n"
                "  public IActionResult Get(int id) { return Ok(id); }\n"
                "}\n",
                encoding="utf-8",
            )
            (source_root / "appsettings.json").write_text(
                json.dumps(
                    {
                        "ConnectionStrings": {
                            "Orders": "Server=localhost;Database=orders"
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "limits": {"extractorTimeoutSeconds": 120},
                        "sources": [
                            {
                                "name": "api",
                                "type": "dotnet-api",
                                "folders": ["."],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            index = json.loads(
                (output / "graph-index.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "GET /api/Orders/{id}",
                index["apisByMethodPath"],
                KnowledgeStore(output).list_issues(source="api"),
            )
            self.assertEqual(index["edgeTypeCounts"]["HANDLES_API"], 1)
            self.assertEqual(index["nodeTypeCounts"]["CONFIG_KEY"], 2)
            handler_edges = KnowledgeStore(output).find_edges(
                edge_type="HANDLES_API"
            )
            self.assertEqual(len(handler_edges["edge_ids"]), 1)

    @unittest.skipUnless(shutil.which("dotnet"), ".NET SDK is not installed")
    def test_dotnet_framework_explicit_conventional_route_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            source_root.mkdir()
            (source_root / "RouteConfig.cs").write_text(
                "public static class RouteConfig {\n"
                "  public static void RegisterRoutes(dynamic routes) {\n"
                "    routes.MapRoute(\n"
                "      name: \"Default\",\n"
                "      url: \"{controller}/{action}/{id}\",\n"
                "      defaults: new { controller = \"Home\", action = \"Index\" });\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "limits": {"extractorTimeoutSeconds": 120},
                        "sources": [
                            {
                                "name": "legacy-api",
                                "type": "dotnet-api",
                                "folders": ["."],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            index = json.loads(
                (output / "graph-index.json").read_text(encoding="utf-8")
            )
            self.assertIn(
                "ANY /{id}/{id}/{id}",
                index["apisByMethodPath"],
                KnowledgeStore(output).list_issues(source="legacy-api"),
            )
            self.assertEqual(index["nodeTypeCounts"]["API_OPERATION"], 1)

    @unittest.skipUnless(
        shutil.which("dotnet") and importlib.util.find_spec("antlr4"),
        ".NET SDK and ANTLR runtime are required",
    )
    def test_dotnet_database_reference_resolves_from_later_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            api_root = source_root / "api"
            db_root = source_root / "db"
            api_root.mkdir(parents=True)
            db_root.mkdir()
            (api_root / "OrderRepository.cs").write_text(
                "public class OrderRepository {\n"
                "  public void Load() {\n"
                '    const string sql = "SELECT * FROM APP.ORDERS";\n'
                "    System.Console.WriteLine(sql);\n"
                "  }\n"
                "}\n",
                encoding="utf-8",
            )
            (db_root / "placeholder.sql").write_text(
                "BEGIN NULL; END;\n/\n", encoding="utf-8"
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "limits": {"extractorTimeoutSeconds": 120},
                        "sources": [
                            {
                                "name": "api",
                                "type": "dotnet-api",
                                "folders": ["api"],
                                "database": "DB",
                            },
                            {
                                "name": "db",
                                "type": "oracle-plsql",
                                "folders": ["db"],
                                "database": "DB",
                                "schema": "APP",
                                "supplementalNodes": [
                                    {
                                        "nodeId": "table:DB:ORDERS",
                                        "nodeType": "TABLE",
                                        "technicalName": "ORDERS",
                                        "qualifiedName": "DB.APP.ORDERS",
                                        "database": "DB",
                                        "repository": "db",
                                        "properties": {
                                            "database": "DB",
                                            "schema": "APP",
                                            "table": "ORDERS",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            table = _db_rows(
                output, "nodes", "stable_id = ?", ("table:DB:ORDERS",)
            )[0]
            reads = _db_rows(
                output,
                "edges",
                "edge_type = 'READS_FROM' AND target_node_id = ?",
                (table["node_id"],),
            )
            self.assertEqual(len(reads), 1)
            self.assertFalse(
                _db_rows(output, "nodes", "node_type = 'UNRESOLVED_REFERENCE'")
            )
            self.assertNotIn(
                "TABLE_NOT_IMPORTED",
                {issue["issue_type"] for issue in _db_rows(output, "issues")},
            )

    @unittest.skipUnless(
        importlib.util.find_spec("antlr4"),
        "ANTLR runtime is an optional test-environment dependency",
    )
    def test_sql_and_loader_keep_literal_facts_without_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            source_root.mkdir()
            (source_root / "orders.sql").write_text(
                "SELECT * FROM APP.ORDERS;\n"
                "UPDATE APP.ORDERS SET STATUS = 1;\n",
                encoding="utf-8",
            )
            (source_root / "orders.ctl").write_text(
                "LOAD DATA\n"
                "INFILE 'orders.csv'\n"
                "APPEND INTO TABLE APP.ORDERS\n"
                "(ORDER_ID INTEGER EXTERNAL, STATUS CHAR)\n",
                encoding="utf-8",
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "name": "orders",
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "limits": {
                            "maxFileBytes": 1024 * 1024,
                            "extractorTimeoutSeconds": 30,
                        },
                        "sources": [
                            {
                                "name": "sql",
                                "type": "sql-files",
                                "folders": ["orders.sql"],
                                "database": "DB",
                                "schema": "APP",
                            },
                            {
                                "name": "loader",
                                "type": "sql-loader",
                                "folders": ["orders.ctl"],
                                "database": "DB",
                                "schema": "APP",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            index = json.loads(
                (output / "graph-index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["nodeTypeCounts"]["LOADER_CONTROL"], 1)
            self.assertGreaterEqual(
                index["nodeTypeCounts"]["UNRESOLVED_REFERENCE"], 3
            )
            self.assertEqual(index["edgeTypeCounts"]["READS_FROM"], 1)
            self.assertEqual(index["edgeTypeCounts"]["WRITES_TO"], 1)
            self.assertEqual(index["edgeTypeCounts"]["LOADS_INTO"], 1)
            self.assertEqual(index["edgeTypeCounts"]["MAPS_TO"], 2)
            self.assertIn("DB.APP.ORDERS", index["tablesByQName"])

            loader_issues = _db_rows(
                output, "issues", "package_key = ?", ("sources/loader",)
            )
            self.assertIn(
                "TABLE_NOT_IMPORTED",
                {issue["issue_type"] for issue in loader_issues},
            )
            self.assertIn(
                "COLUMN_NOT_IMPORTED",
                {issue["issue_type"] for issue in loader_issues},
            )

            store = KnowledgeStore(output)
            impact = store.impact_table("DB", "APP", "ORDERS")
            self.assertGreaterEqual(len(impact["node_ids"]), 2)
            self.assertTrue(
                (output / "knowledge/Databases.md").read_text(encoding="utf-8")
            )

    @unittest.skipUnless(
        importlib.util.find_spec("antlr4"),
        "ANTLR runtime is an optional test-environment dependency",
    )
    def test_plsql_routine_keeps_statement_facts_without_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            source_root = workspace / "source"
            source_root.mkdir()
            (source_root / "orders.pkb").write_text(
                "CREATE OR REPLACE PACKAGE BODY order_pkg AS\n"
                "  PROCEDURE load_orders IS\n"
                "    v_id NUMBER;\n"
                "  BEGIN\n"
                "    v_id := 0;\n"
                "    INSERT INTO APP.ORDERS(ID)\n"
                "      VALUES (APP.ORDER_SEQ.NEXTVAL);\n"
                "    SELECT ID INTO v_id FROM APP.CUSTOMERS;\n"
                "  END;\n"
                "END order_pkg;\n"
                "/\n",
                encoding="utf-8",
            )
            output = workspace / "output"
            config_path = workspace / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "root": str(source_root),
                        "output": str(output),
                        "outputMode": "partitioned",
                        "sources": [
                            {
                                "name": "plsql",
                                "type": "oracle-plsql",
                                "folders": ["."],
                                "database": "DB",
                                "schema": "APP",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            run_pipeline(config_path)

            index = json.loads(
                (output / "graph-index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["edgeTypeCounts"]["READS_FROM"], 1)
            self.assertEqual(index["edgeTypeCounts"]["WRITES_TO"], 1)
            self.assertEqual(index["edgeTypeCounts"]["USES_SEQUENCE"], 1)
            self.assertEqual(
                index["nodeTypeCounts"]["UNRESOLVED_REFERENCE"], 2
            )
            self.assertIn("DB.APP.ORDERS", index["tablesByQName"])
            self.assertIn("DB.APP.CUSTOMERS", index["tablesByQName"])
            nodes = _db_rows(
                output, "nodes", "package_key = ?", ("sources/plsql",)
            )
            routine = next(row for row in nodes if row["node_type"] == "PROCEDURE")
            semantic_tree = json.loads(routine["properties_json"])["semantic_tree"]
            self.assertNotIn("assignment", _semantic_fact_types(semantic_tree["steps"]))
            self.assertEqual(
                _semantic_fact_types(semantic_tree["steps"]),
                {"data_effect"},
            )


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


def _semantic_fact_types(facts: list[dict]) -> set[str]:
    result = set()
    pending = list(facts)
    while pending:
        fact = pending.pop()
        result.add(str(fact.get("type")))
        for key in (
            "steps",
            "else_steps",
            "catches",
            "finally_steps",
            "cases",
            "effects",
        ):
            children = fact.get(key)
            if isinstance(children, list):
                pending.extend(children)
    return result


if __name__ == "__main__":
    unittest.main()
