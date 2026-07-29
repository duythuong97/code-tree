from __future__ import annotations

import json
import re

from code_tree_exporter.extractors.package_support.semantic_tree_v3 import analysis_notes as _analysis_notes_v3
from code_tree_exporter.extractors.package_support.semantic_tree_v3 import plsql_steps, sql_facts


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
    detail: str = "summary",
) -> None:
    tree = {
        "version": 3,
        "type": "operation",
        "label": name,
        "summary": "",
        "parameters": _parameters(parameter_block, signature),
        "steps": plsql_steps(
            builder,
            owner_id,
            text,
            source_path,
            base_line,
            detail=detail,
        ),
        "analysis_notes": _analysis_notes_v3(builder, owner_id, source_path, base_line),
    }
    _set_tree(builder, owner_id, tree)


def attach_sql_semantic_tree(builder, owner_id: str, name: str, text: str, source_path: str) -> None:
    tree = {
        "version": 3,
        "type": "operation",
        "label": name,
        "summary": "",
        "parameters": [],
        "steps": sql_facts(builder, owner_id, text, source_path, 1),
        "analysis_notes": _analysis_notes_v3(builder, owner_id, source_path, 1),
    }
    _set_tree(builder, owner_id, tree)


def _parameters(block: str | None, signature: str) -> list[dict]:
    if not block:
        return [] if signature == "void" else [{"name": "", "type": value} for value in signature.split("_") if value]
    result = []
    for raw in _split_top_level(block.strip()[1:-1]):
        match = re.match(r"\s*([A-Za-z_][\w$#]*)\s+(?:(IN\s+OUT|IN|OUT)(?:\s+NOCOPY)?\s+)?(.+?)\s*$", raw, re.IGNORECASE)
        if not match:
            continue
        type_text, default = _split_default(match.group(3))
        item = {"name": match.group(1), "type": " ".join(type_text.split()).upper()}
        if match.group(2):
            item["direction"] = " ".join(match.group(2).upper().split())
        if default is not None:
            item["default"] = " ".join(default.split())
        result.append(item)
    return result


def _split_top_level(value: str) -> list[str]:
    parts, start, depth, quote = [], 0, 0, False
    for index, char in enumerate(value):
        if char == "'":
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")":
            depth = max(0, depth - 1)
        elif not quote and char == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _split_default(value: str) -> tuple[str, str | None]:
    depth, quote = 0, False
    for index, char in enumerate(value):
        if char == "'":
            quote = not quote
        elif not quote and char == "(":
            depth += 1
        elif not quote and char == ")":
            depth = max(0, depth - 1)
        elif not quote and depth == 0:
            rest = value[index:]
            match = re.match(r"\s*(?:DEFAULT\b|:=)\s*", rest, re.IGNORECASE)
            if match:
                return value[:index], rest[match.end():]
    return value, None









def _set_tree(builder, owner_id: str, tree: dict) -> None:
    row = builder.nodes[owner_id]
    properties = json.loads(row["properties_json"])
    properties["semantic_tree"] = tree
    row["properties_json"] = json.dumps(properties, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
