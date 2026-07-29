from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from code_tree_exporter.contract.graph_contract import (
    canonical_edge_id,
    stable_node_id,
)

if TYPE_CHECKING:
    from code_tree_exporter.graph_package import GraphPackage


_PROJECT_TYPES = frozenset({"DOTNET_SOLUTION", "DOTNET_PROJECT"})
_DATABASE_MODULE_TYPES = frozenset(
    {
        "PLSQL_PACKAGE",
        "PACKAGE",
        "PROCEDURE",
        "FUNCTION",
        "TRIGGER",
        "SQL_FILE",
        "XML_SQL_MAPPER",
        "LOADER_CONTROL",
    }
)
_SYSTEM_ROOT_TYPES = frozenset(
    {
        "APPLICATION",
        "API_APPLICATION",
        "ANGULAR_PROJECT",
        "DOTNET_SOLUTION",
        "DOTNET_PROJECT",
        "JOB_NETWORK",
        "DATABASE",
    }
)


def enrich_hierarchy(graph: GraphPackage) -> None:
    """Add only missing coarse structural links after all source packages merge."""
    _ensure_system_nodes(graph)
    _attach_api_projects(graph)
    _attach_database_modules(graph)
    _attach_system_roots(graph)


def _ensure_system_nodes(graph: GraphPackage) -> None:
    systems = {
        str(node.get("system_key") or "").strip()
        for node in graph.nodes.values()
        if str(node.get("system_key") or "").strip()
    }
    systems.update(
        str(source.get("system_key") or "").strip()
        for source in graph.source_descriptors.values()
        if str(source.get("system_key") or "").strip()
    )
    for system_key in sorted(systems):
        node_id = stable_node_id("system", system_key)
        graph.nodes.setdefault(
            node_id,
            {
                "node_id": node_id,
                "node_type": "SYSTEM",
                "technical_name": system_key,
                "qualified_name": system_key,
                "default_display_name": system_key,
                "system_key": system_key,
                "database_key": "",
                "repository_key": "",
                "graph_role": "MAIN",
                "confidence": "1.0",
                "properties_json": "{}",
            },
        )


def _attach_api_projects(graph: GraphPackage) -> None:
    applications: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node_id, node in graph.nodes.items():
        if node.get("node_type") == "API_APPLICATION":
            applications[_scope(node)].append(node_id)
    children = _structural_children(graph)
    for node_id, node in sorted(graph.nodes.items()):
        if node.get("node_type") not in _PROJECT_TYPES or node_id in children:
            continue
        owners = sorted(applications.get(_scope(node), ()))
        if owners:
            _put_contains(graph, owners[0], node_id)


def _attach_database_modules(graph: GraphPackage) -> None:
    databases = {
        str(node.get("database_key") or node.get("technical_name") or "").upper(): node_id
        for node_id, node in graph.nodes.items()
        if node.get("node_type") == "DATABASE"
    }
    children = _structural_children(graph)
    for node_id, node in sorted(graph.nodes.items()):
        database = str(node.get("database_key") or "").upper()
        if (
            node.get("node_type") in _DATABASE_MODULE_TYPES
            and database in databases
            and node_id not in children
        ):
            _put_contains(graph, databases[database], node_id)


def _attach_system_roots(graph: GraphPackage) -> None:
    children = _structural_children(graph)
    for node_id, node in sorted(graph.nodes.items()):
        node_type = str(node.get("node_type") or "")
        system_key = str(node.get("system_key") or "").strip()
        if (
            node_type not in _SYSTEM_ROOT_TYPES
            or node_type == "SYSTEM"
            or not system_key
            or node_id in children
        ):
            continue
        _put_contains(graph, stable_node_id("system", system_key), node_id)


def _scope(node: dict[str, str]) -> tuple[str, str]:
    return (
        str(node.get("system_key") or ""),
        str(node.get("repository_key") or ""),
    )


def _structural_children(graph: GraphPackage) -> set[str]:
    return {
        str(edge.get("target_node_id") or "")
        for edge in graph.edges.values()
        if edge.get("edge_type") == "CONTAINS"
        and edge.get("graph_layer") == "STRUCTURAL"
    }


def _put_contains(graph: GraphPackage, source_id: str, target_id: str) -> None:
    edge_id = canonical_edge_id(
        source_id, "CONTAINS", target_id, "", "STRUCTURAL"
    )
    graph.edges.setdefault(
        edge_id,
        {
            "edge_id": edge_id,
            "source_node_id": source_id,
            "target_node_id": target_id,
            "edge_type": "CONTAINS",
            "graph_layer": "STRUCTURAL",
            "raw_operation": "",
            "confidence": "1.0",
            "properties_json": "{}",
        },
    )
