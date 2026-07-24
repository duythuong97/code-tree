from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path, PurePosixPath

from contract.graph_contract import stable_node_id
from graph_package import GraphPackage

_REFERENCE_NODE_TYPES = frozenset(
    {"DATA_FILE", "SEQUENCE", "TABLE", "UNRESOLVED_REFERENCE"}
)
_EXPANDED_EDGE_TYPES = frozenset(
    {"CALLS", "READS", "INSERTS", "UPDATES", "DELETES", "MERGES", "USES_SEQUENCE"}
)


def render_file_tree(package: GraphPackage, output: Path) -> None:
    records = sorted(
        package.source_records, key=lambda item: (item.source_key, item.relative_path)
    )
    declarations: dict[str, dict[str, str]] = {}
    edge_evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
    for evidence in package.evidence.values():
        if evidence.get("target_type") == "NODE":
            current = declarations.get(evidence["target_id"])
            if current is None or _line(evidence) < _line(current):
                declarations[evidence["target_id"]] = evidence
        elif evidence.get("target_type") == "EDGE":
            edge_evidence[evidence["target_id"]].append(evidence)
    children: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in package.edges.values():
        if edge["edge_type"] == "CONTAINS" and edge["graph_layer"] == "STRUCTURAL":
            children[edge["source_node_id"]].append(edge["target_node_id"])
        else:
            outgoing[edge["source_node_id"]].append(edge)
    comments: dict[str, list[dict[str, str]]] = defaultdict(list)
    for comment in package.comments.values():
        comments[comment["owner_node_id"]].append(comment)

    output.mkdir(parents=True, exist_ok=True)
    output_paths = [
        PurePosixPath(record.relative_path).with_suffix(".md") for record in records
    ]
    collisions = {path for path in output_paths if output_paths.count(path) > 1}
    for record, relative_output in zip(records, output_paths):
        lines = [f"# File Tree: {record.relative_path}", ""]
        file_id = stable_node_id("file", record.source_key, record.relative_path)
        lines.append(
            f"- FILE: `{record.relative_path}` [{record.actual_encoding}; {record.newline_style}]"
        )
        for node_id in sorted(
            children.get(file_id, []),
            key=lambda value: _node_order(value, package, declarations),
        ):
            _render_node(
                lines,
                package,
                node_id,
                1,
                children,
                outgoing,
                declarations,
                edge_evidence,
                comments,
                frozenset(),
            )
        for comment in sorted(comments.get(file_id, []), key=_line):
            _render_comment(lines, comment, 1)
        if relative_output in collisions:
            relative_output = PurePosixPath(record.relative_path + ".md")
        target = output.joinpath(*relative_output.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _render_node(
    lines,
    package,
    node_id,
    depth,
    children,
    outgoing,
    declarations,
    edge_evidence,
    comments,
    active,
) -> None:
    if node_id not in package.nodes:
        return
    if node_id in active:
        lines.append(
            f"{'  ' * depth}- ↩ reference: `{package.nodes[node_id]['qualified_name']}`"
        )
        return
    active = active | {node_id}
    node = package.nodes[node_id]
    lines.append(
        f"{'  ' * depth}- {node['node_type']}: `{node['qualified_name']}`{_location(declarations.get(node_id, {}))}"
    )
    for comment in sorted(comments.get(node_id, []), key=_line):
        _render_comment(lines, comment, depth + 1)
    _render_semantic_tree(lines, node, depth + 1)
    for child_id in sorted(
        children.get(node_id, []),
        key=lambda value: _node_order(value, package, declarations),
    ):
        _render_node(
            lines,
            package,
            child_id,
            depth + 1,
            children,
            outgoing,
            declarations,
            edge_evidence,
            comments,
            active,
        )
    for edge in sorted(
        outgoing.get(node_id, []),
        key=lambda row: (
            _edge_line(row, edge_evidence),
            row["edge_type"],
            row["target_node_id"],
        ),
    ):
        target = package.nodes.get(edge["target_node_id"])
        if not target:
            continue
        rows = sorted(edge_evidence.get(edge["edge_id"], []), key=_line)
        lines.append(
            f"{'  ' * (depth + 1)}- {edge['edge_type']}: `{target['qualified_name']}`{_location(rows[0]) if rows else ''}"
        )
        if (
            edge["edge_type"] in _EXPANDED_EDGE_TYPES
            and target["node_type"] not in _REFERENCE_NODE_TYPES
        ):
            _render_node(
                lines,
                package,
                target["node_id"],
                depth + 2,
                children,
                outgoing,
                declarations,
                edge_evidence,
                comments,
                active,
            )


def _render_semantic_tree(lines: list[str], node: dict[str, str], depth: int) -> None:
    try:
        tree = json.loads(node.get("properties_json") or "{}").get("semantic_tree")
    except json.JSONDecodeError:
        return
    if not isinstance(tree, dict):
        return
    for parameter in tree.get("parameters", []):
        if isinstance(parameter, dict):
            detail = " ".join(
                filter(
                    None,
                    (
                        parameter.get("direction", ""),
                        parameter.get("name", ""),
                        parameter.get("type", ""),
                    ),
                )
            )
            lines.append(f"{'  ' * depth}- PARAMETER: `{detail}`")
    for group in ("steps", "outputs", "exceptions", "analysis_notes"):
        for fact in tree.get(group, []):
            _render_fact(lines, fact, depth)


def _render_fact(lines: list[str], fact, depth: int) -> None:
    if not isinstance(fact, dict):
        return
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    location = f" [L{source['line']}]" if source.get("line") else ""
    lines.append(
        f"{'  ' * depth}- {str(fact.get('type') or 'STEP').upper()}: {fact.get('label', '')}{location}"
    )
    for child in fact.get("steps", []):
        _render_fact(lines, child, depth + 1)


def _render_comment(lines: list[str], comment: dict[str, str], depth: int) -> None:
    text = " ".join(comment.get("normalized_text", "").split())
    lines.append(
        f"{'  ' * depth}- COMMENT [{comment.get('comment_kind', 'COMMENT')}; L{comment.get('start_line', '?')}]: {text}"
    )


def _line(row: dict[str, str]) -> int:
    value = row.get("start_line", "")
    return int(value) if value.isdigit() else 0


def _location(evidence: dict[str, str]) -> str:
    start = evidence.get("start_line", "")
    end = evidence.get("end_line", "")
    if not start:
        return ""
    return f" [L{start}{'-L' + end if end and end != start else ''}]"


def _node_order(
    node_id: str, package: GraphPackage, declarations: dict[str, dict[str, str]]
) -> tuple:
    node = package.nodes.get(node_id, {})
    return (
        _line(declarations.get(node_id, {})),
        node.get("node_type", ""),
        node.get("qualified_name", ""),
    )


def _edge_line(edge: dict[str, str], evidence: dict[str, list[dict[str, str]]]) -> int:
    return min((_line(row) for row in evidence.get(edge["edge_id"], [])), default=0)


def render_system_tree(package: GraphPackage, output: Path) -> None:
    nodes_by_system: dict[str, list[dict[str, str]]] = defaultdict(list)
    for node in package.nodes.values():
        system = node.get("system_key") or "unassigned"
        if node.get("node_type") not in {"FILE", "SYSTEM"}:
            nodes_by_system[system].append(node)
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in package.edges.values():
        if edge["edge_type"] != "CONTAINS":
            outgoing[edge["source_node_id"]].append(edge)

    lines = ["# System Tree", ""]
    for system in sorted(nodes_by_system):
        lines.extend([f"## {system}", ""])
        for node in sorted(
            nodes_by_system[system],
            key=lambda item: (item["node_type"], item["qualified_name"]),
        ):
            lines.append(f"- {node['node_type']}: `{node['qualified_name']}`")
            for edge in sorted(
                outgoing.get(node["node_id"], []),
                key=lambda item: (item["edge_type"], item["target_node_id"]),
            ):
                target = package.nodes.get(edge["target_node_id"])
                target_name = (
                    target["qualified_name"] if target else edge["target_node_id"]
                )
                lines.append(f"  - {edge['edge_type']} → `{target_name}`")
        lines.append("")
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
