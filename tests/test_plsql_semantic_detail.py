from __future__ import annotations

import importlib.util
import unittest

from code_tree_exporter.extractors.package_support.semantic_tree_v3 import plsql_steps


class _Builder:
    nodes: dict = {}
    edges: dict = {}
    issues: dict = {}


@unittest.skipUnless(
    importlib.util.find_spec("antlr4"),
    "ANTLR runtime is an optional test-environment dependency",
)
class PlsqlSemanticDetailTests(unittest.TestCase):
    def test_summary_keeps_dependencies_without_statement_noise(self) -> None:
        text = (
            "CREATE OR REPLACE PROCEDURE sync_order(p_id IN NUMBER) IS\n"
            "  v_status VARCHAR2(20) := 'NEW';\n"
            "BEGIN\n"
            "  v_status := 'READY';\n"
            "  IF p_id IS NOT NULL THEN\n"
            "    INSERT INTO app.orders(id, status) VALUES (p_id, v_status);\n"
            "    audit_pkg.log_event(p_id);\n"
            "  END IF;\n"
            "  RETURN;\n"
            "END;\n"
        )

        summary = plsql_steps(
            _Builder(),
            "procedure:test",
            text,
            "sync_order.prc",
            1,
            detail="summary",
        )
        full = plsql_steps(
            _Builder(),
            "procedure:test",
            text,
            "sync_order.prc",
            1,
            detail="full",
        )

        self.assertEqual(_fact_types(summary), {"branch", "call", "data_effect"})
        self.assertIn("assignment", _fact_types(full))
        self.assertIn("output", _fact_types(full))
        call = next(fact for fact in _walk(summary) if fact["type"] == "call")
        self.assertNotIn("arguments", call)


def _walk(facts: list[dict]):
    for fact in facts:
        yield fact
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
                yield from _walk(children)


def _fact_types(facts: list[dict]) -> set[str]:
    return {str(fact.get("type")) for fact in _walk(facts)}


if __name__ == "__main__":
    unittest.main()
