from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path, PurePosixPath

from .graph_package import GraphPackage

_FACT_CHILD_GROUPS = (
    ("initialization_steps", "INITIALIZE"),
    ("condition_steps", "CONDITION"),
    ("steps", "DO"),
    ("increment_steps", "INCREMENT"),
    ("cases", "CASE"),
    ("else_steps", "ELSE"),
    ("catches", "CATCH"),
    ("finally_steps", "FINALLY"),
    ("effects", "EFFECTS"),
)


def render_file_tree(package: GraphPackage, output: Path, max_lines: int = 20_000) -> None:
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
        nodes_in_file = {
            evidence["target_id"]
            for evidence in package.evidence.values()
            if evidence.get("target_type") == "NODE" and evidence.get("source_path") == record.relative_path
        }

        local_comments = sorted(
            (comment for comment in package.comments.values() if comment["source_path"] == record.relative_path),
            key=lambda item: (_line(item), item["comment_id"]),
        )

        if not nodes_in_file:
            continue

        file_root_nodes = []
        for node_id in nodes_in_file:
            is_child = any(
                node_id in children.get(parent_id, [])
                for parent_id in nodes_in_file
            )
            if not is_child:
                file_root_nodes.append(node_id)

        lines = [f"# File Tree: {record.relative_path}", ""]
        lines.append(
            f"- FILE: `{record.relative_path}` [{record.actual_encoding}; {record.newline_style}]"
        )
        for node_id in sorted(
            file_root_nodes,
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
                record.relative_path,
            )
        if local_comments:
            counts: dict[str, int] = defaultdict(int)
            for comment in local_comments:
                try:
                    classification = json.loads(comment.get("properties_json") or "{}").get("classification", "OTHER")
                except json.JSONDecodeError:
                    classification = "OTHER"
                counts[str(classification)] += 1
            summary = ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            lines.append(f"  - COMMENTS: {len(local_comments)} [{summary}] (full text: graph.sqlite comments)")
        if relative_output in collisions:
            relative_output = PurePosixPath(record.relative_path + ".md")
        target = output.joinpath(*relative_output.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_limited_text(lines, max_lines), encoding="utf-8")


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
    source_path,
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
            source_path,
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
        target_name = target["qualified_name"] if target else edge["target_node_id"]
        rows = sorted(edge_evidence.get(edge["edge_id"], []), key=_line)
        lines.append(
            f"{'  ' * (depth + 1)}- {edge['edge_type']}: `{target_name}`{_location(rows[0]) if rows else ''}"
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
    details = []
    for key in (
        "condition", "iterator", "target", "expression", "then_expression",
        "else_expression", "action", "result", "resolution", "ref_node_id",
        "awaited", "code", "severity",
    ):
        value = fact.get(key)
        if value not in (None, "", False):
            details.append(f"{key}={value}")
    for key in ("arguments", "ref_node_ids"):
        if fact.get(key):
            details.append(f"{key}={json.dumps(fact[key], ensure_ascii=False, separators=(',', ':'))}")
    suffix = f" [{'; '.join(details)}]" if details else ""
    lines.append(
        f"{'  ' * depth}- {str(fact.get('type') or 'STEP').upper()}: {fact.get('label', '')}{suffix}{location}"
    )
    for parameter in fact.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        detail = " ".join(filter(None, (parameter.get("direction", ""), parameter.get("name", ""), parameter.get("type", ""))))
        lines.append(f"{'  ' * (depth + 1)}- PARAMETER: `{detail}`")
    for key, label in _FACT_CHILD_GROUPS:
        children = fact.get(key, [])
        if not isinstance(children, list) or not children:
            continue
        lines.append(f"{'  ' * (depth + 1)}- {label}:")
        for child in children:
            _render_fact(lines, child, depth + 2)


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


def render_system_tree(package: GraphPackage, output: Path, max_lines: int = 20_000) -> None:
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
    output.write_text(_limited_text(lines, max_lines), encoding="utf-8")


def _limited_text(lines: list[str], max_lines: int) -> str:
    if max_lines < 10:
        raise ValueError("maxTreeLines must be at least 10")
    if len(lines) > max_lines:
        lines = lines[: max_lines - 2] + ["", "_Projection truncated; query graph.sqlite for the complete graph._"]
    return "\n".join(lines).rstrip() + "\n"
