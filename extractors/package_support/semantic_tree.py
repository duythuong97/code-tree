from __future__ import annotations

import json
import re

from extractors.package_support.package_writer import line_for_offset
from extractors.package_support.sql_analyzer import analyze_sql


def attach_plsql_semantic_tree(
    builder,
    owner_id: str,
    kind: str,
    name: str,
    signature: str,
    *,
    parameter_block: str | None = None,
    text: str = "",
    source_path: str = "",
    base_line: int = 1,
) -> None:
    """Attach source-ordered V2 facts; full control-flow analysis stays out of scope."""
    facts = _facts(builder, owner_id, text, source_path, base_line)
    exception_match = re.search(r"\bEXCEPTION\b", text, re.IGNORECASE)
    exception_start = exception_match.start() if exception_match else len(text)
    tree = {
        "version": 2,
        "type": "operation",
        "label": name,
        "summary": "",
        "parameters": _parameters(parameter_block, signature),
        "steps": [fact for offset, fact in facts if offset < exception_start],
        "outputs": [fact for offset, fact in facts if fact["type"] == "return"],
        "exceptions": _exception_handlers(text, facts, source_path, base_line),
        "analysis_notes": _analysis_notes(builder, owner_id),
    }
    _set_tree(builder, owner_id, tree)


def attach_sql_semantic_tree(builder, owner_id: str, name: str, text: str, source_path: str) -> None:
    tree = {
        "version": 2,
        "type": "operation",
        "label": name,
        "summary": "",
        "parameters": [],
        "steps": [fact for _, fact in _facts(builder, owner_id, text, source_path, 1)],
        "outputs": [],
        "exceptions": [],
        "analysis_notes": _analysis_notes(builder, owner_id),
    }
    _set_tree(builder, owner_id, tree)


def _parameters(block: str | None, signature: str) -> list[dict]:
    if not block:
        return [] if signature == "void" else [{"name": "", "type": value} for value in signature.split("_") if value]
    result = []
    for raw in block.strip()[1:-1].split(","):
        match = re.match(r"\s*([A-Za-z_][\w$#]*)\s+(?:(IN\s+OUT|IN|OUT)\s+)?(.+?)(?:\s+(?:DEFAULT|:=)\s+.+)?\s*$", raw, re.IGNORECASE)
        if not match:
            continue
        item = {"name": match.group(1), "type": " ".join(match.group(3).split()).upper()}
        if match.group(2):
            item["direction"] = " ".join(match.group(2).upper().split())
        result.append(item)
    return result


def _facts(builder, owner_id: str, text: str, source_path: str, base_line: int) -> list[tuple[int, dict]]:
    facts = []
    for ref in analyze_sql(text).tables:
        target = _target(builder, owner_id, ref.object_name, ref.edge_type)
        fact = _fact("data_effect", f"{ref.operation} {ref.object_name.upper()}", ref.start, text, source_path, base_line, action=ref.operation)
        if target:
            fact["ref_node_id"] = target
        facts.append((ref.start, fact))
    for match in re.finditer(r"\b([A-Za-z_][\w$#]*)\s*:=", text, re.IGNORECASE):
        facts.append((match.start(), _fact("assignment", f"Set {match.group(1)}", match.start(), text, source_path, base_line, action=":=")))
    body = re.search(r"\bBEGIN\b", text, re.IGNORECASE)
    body_start = body.end() if body else 0
    for match in re.finditer(r"\bRETURN\b\s+(.+?);", text, re.IGNORECASE | re.DOTALL):
        if match.start() < body_start:
            continue
        facts.append((match.start(), _fact("return", f"Return {_compact(match.group(1))}", match.start(), text, source_path, base_line)))
    for edge in builder.edges.values():
        if edge["source_node_id"] != owner_id or edge["edge_type"] not in {"CALLS", "CALLS_API"}:
            continue
        target = builder.nodes.get(edge["target_node_id"], {})
        name = target.get("technical_name") or edge["target_node_id"]
        match = re.search(rf"\b{re.escape(name)}\s*\(", text, re.IGNORECASE)
        if match:
            fact = _fact("call", f"Call {name}", match.start(), text, source_path, base_line)
            fact["ref_node_id"] = edge["target_node_id"]
            facts.append((match.start(), fact))
    return sorted(facts, key=lambda item: (item[0], item[1]["type"], item[1]["label"]))


def _exception_handlers(text: str, facts: list[tuple[int, dict]], source_path: str, base_line: int) -> list[dict]:
    exception = re.search(r"\bEXCEPTION\b", text, re.IGNORECASE)
    if not exception:
        return []
    matches = list(re.finditer(r"\bWHEN\s+(.+?)\s+THEN\b", text[exception.end():], re.IGNORECASE | re.DOTALL))
    handlers = []
    for index, match in enumerate(matches):
        start = exception.end() + match.start()
        body_start = exception.end() + match.end()
        end = exception.end() + (matches[index + 1].start() if index + 1 < len(matches) else len(text) - exception.end())
        handlers.append({"type": "exception", "label": _compact(match.group(1)), "source": _source(source_path, text, base_line, start), "steps": [fact for offset, fact in facts if body_start <= offset < end]})
    return handlers


def _analysis_notes(builder, owner_id: str) -> list[dict]:
    issues = [issue for issue in builder.issues.values() if issue["source_node_id"] == owner_id]
    return [{"type": "analysis_note", "code": issue["issue_type"], "severity": issue["severity"], "label": issue["message"]} for issue in sorted(issues, key=lambda row: (int(row.get("start_line") or 0), row["issue_type"]))]


def _target(builder, owner_id: str, object_name: str, edge_type: str) -> str | None:
    leaf = object_name.upper().split("@", 1)[0].rsplit(".", 1)[-1]
    candidates = []
    for edge in builder.edges.values():
        if edge["source_node_id"] != owner_id or edge["edge_type"] != edge_type:
            continue
        target = builder.nodes.get(edge["target_node_id"], {})
        if (target.get("technical_name") or "").upper() == leaf:
            candidates.append(edge["target_node_id"])
    return candidates[0] if len(candidates) == 1 else None


def _fact(kind: str, label: str, offset: int, text: str, source_path: str, base_line: int, **extra) -> dict:
    return {"type": kind, "label": label, "source": _source(source_path, text, base_line, offset), **extra}


def _source(path: str, segment: str, base_line: int, offset: int) -> dict:
    return {"path": path, "line": base_line + line_for_offset(segment, offset) - 1}


def _compact(value: str) -> str:
    return " ".join(value.split())[:180]


def _set_tree(builder, owner_id: str, tree: dict) -> None:
    row = builder.nodes[owner_id]
    properties = json.loads(row["properties_json"])
    properties["semantic_tree"] = tree
    row["properties_json"] = json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
