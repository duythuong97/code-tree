from __future__ import annotations

from collections import defaultdict
from typing import Any

from contract import schema as S
from contract.entities import ExtractionResult, GraphEdge, GraphNode

_TABLE_ACTIONS = {
    S.REL_READS_FROM: "READ",
    S.REL_INSERTS_INTO: "WRITE",
    S.REL_UPDATES: "WRITE",
    S.REL_DELETES_FROM: "WRITE",
    S.REL_MERGES_INTO: "WRITE",
    S.REL_WRITES_TO: "WRITE",
}


def attach_edge_semantics(result: ExtractionResult) -> None:
    """Attach semantic metadata using facts already emitted by extractors."""
    nodes = {node.key_value: node for node in result.nodes}
    reads_by_statement: dict[tuple[str, int, str], list[GraphEdge]] = defaultdict(list)
    writes_by_statement: dict[tuple[str, int, str], list[GraphEdge]] = defaultdict(list)
    derivations: dict[tuple[str, int, str], list[GraphEdge]] = defaultdict(list)

    for edge in result.edges:
        line = _line(edge)
        operation = _operation_family(edge.properties.get("operation", ""))
        key = (edge.from_key_value, line, operation)
        if edge.rel_type == "READS_COLUMN":
            reads_by_statement[key].append(edge)
        elif edge.rel_type == "WRITES_COLUMN":
            writes_by_statement[key].append(edge)
        elif edge.rel_type == "DERIVES_FROM":
            derivations[(edge.to_key_value, line, operation)].append(edge)

    for edge in result.edges:
        if "semantic" in edge.properties:
            continue
        semantic = _semantic_for_edge(
            edge, nodes, reads_by_statement, writes_by_statement, derivations
        )
        if semantic:
            edge.properties["semantic"] = semantic


def _semantic_for_edge(
    edge: GraphEdge,
    nodes: dict[str, GraphNode],
    reads_by_statement: dict[tuple[str, int, str], list[GraphEdge]],
    writes_by_statement: dict[tuple[str, int, str], list[GraphEdge]],
    derivations: dict[tuple[str, int, str], list[GraphEdge]],
) -> dict[str, Any] | None:
    operation = _operation_family(edge.properties.get("operation", ""))
    line = _line(edge)
    location = _location(edge)

    if edge.rel_type in _TABLE_ACTIONS:
        action = _TABLE_ACTIONS[edge.rel_type]
        fields = _table_fields(
            edge,
            nodes,
            reads_by_statement,
            writes_by_statement,
            derivations,
            action,
            line,
            operation,
        )
        statement = {"operation": operation or action, **location, "fields": fields}
        return {
            "version": 1,
            "action": action,
            "operation": operation or action,
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            "fields": fields,
            "statements": [statement],
        }

    if edge.rel_type == S.REL_CALLS:
        return {
            "version": 1,
            "action": "CALL",
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            "call_type": edge.properties.get("call_type", "routine"),
            "resolution": edge.properties.get("resolution", "external"),
            **location,
        }
    if edge.rel_type == S.REL_USES_SEQUENCE:
        return {
            "version": 1,
            "action": "USE_SEQUENCE",
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            **location,
        }
    if edge.rel_type == S.REL_TRIGGERS:
        return {
            "version": 1,
            "action": "TRIGGER_ON",
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            "framework": edge.properties.get("framework", ""),
            **location,
        }
    if edge.rel_type == "HANDLES_EXCEPTION":
        return {
            "version": 1,
            "action": "HANDLE_EXCEPTION",
            "handler": edge.properties.get("handler", ""),
            **location,
        }
    if edge.rel_type in {"READS_COLUMN", "WRITES_COLUMN", "POPULATES"}:
        action = {
            "READS_COLUMN": "READ_FIELD",
            "WRITES_COLUMN": "WRITE_FIELD",
            "POPULATES": "POPULATE_FIELD",
        }[edge.rel_type]
        return {
            "version": 1,
            "action": action,
            "operation": operation,
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            "expression": edge.properties.get("expression", ""),
            **location,
        }
    if edge.rel_type == "DERIVES_FROM":
        return {
            "version": 1,
            "action": "DERIVE_FIELD",
            "operation": operation,
            "source": _node_ref(edge.from_key_value, edge.from_label, nodes),
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            "expression": edge.properties.get("expression", ""),
            **location,
        }
    if edge.rel_type in {S.REL_BELONGS_TO, S.REL_CONTAINS}:
        return {
            "version": 1,
            "action": "CONTAIN" if edge.rel_type == S.REL_CONTAINS else "BELONG_TO",
            "source": _node_ref(edge.from_key_value, edge.from_label, nodes),
            "target": _node_ref(edge.to_key_value, edge.to_label, nodes),
            **location,
        }
    return None


def _table_fields(
    edge: GraphEdge,
    nodes: dict[str, GraphNode],
    reads_by_statement: dict[tuple[str, int, str], list[GraphEdge]],
    writes_by_statement: dict[tuple[str, int, str], list[GraphEdge]],
    derivations: dict[tuple[str, int, str], list[GraphEdge]],
    action: str,
    line: int,
    operation: str,
) -> list[dict[str, Any]]:
    key = (edge.from_key_value, line, operation)
    candidates = (
        reads_by_statement.get(key, [])
        if action == "READ"
        else writes_by_statement.get(key, [])
    )
    if action == "READ" and not candidates:
        candidates = [
            item
            for (owner, item_line, _), items in reads_by_statement.items()
            if owner == edge.from_key_value and item_line == line
            for item in items
        ]
    fields: list[dict[str, Any]] = []
    for item in candidates:
        node = nodes.get(item.to_key_value)
        if not node or node.properties.get("table_qname") != edge.to_key_value:
            continue
        field = {
            "name": node.properties.get("name", item.to_key_value.rsplit(":", 1)[-1]),
            "column_id": item.to_key_value,
            "expression": item.properties.get("expression", ""),
        }
        if action == "WRITE":
            field["sources"] = [
                source.from_key_value
                for source in derivations.get((item.to_key_value, line, operation), [])
            ]
        fields.append(field)
    if fields:
        return _unique_fields(fields)
    return [
        {"name": str(name), "column_id": "", "expression": ""}
        for name in edge.properties.get("columns", [])
        if name
    ]


def _unique_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for field in fields:
        key = (
            field.get("column_id"),
            field.get("expression"),
            tuple(field.get("sources", [])),
        )
        unique.setdefault(key, field)
    return list(unique.values())


def _node_ref(identity: str, label: str, nodes: dict[str, GraphNode]) -> dict[str, Any]:
    node = nodes.get(identity)
    return {
        "id": identity,
        "type": label,
        "name": (
            node.properties.get("name", identity.rsplit(":", 1)[-1])
            if node
            else identity.rsplit(":", 1)[-1]
        ),
    }


def _operation_family(value: Any) -> str:
    operation = str(value).upper()
    for family in ("INSERT", "UPDATE", "DELETE", "MERGE", "SELECT"):
        if operation.startswith(family):
            return family
    return operation


def _line(edge: GraphEdge) -> int:
    try:
        return int(edge.properties.get("line") or 0)
    except (TypeError, ValueError):
        return 0


def _location(edge: GraphEdge) -> dict[str, Any]:
    return {
        "source_path": edge.properties.get("source_path")
        or edge.properties.get("source_file", ""),
        "line": _line(edge),
    }
