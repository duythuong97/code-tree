from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from contract import schema as S
from contract.entities import ExtractionContext, ExtractionResult, GraphEdge, GraphNode
from contract.graph_contract import normalize_repository_path
from extractors.base import BaseExtractor
from extractors.sql.references import (
    extract_sql_calls,
    extract_sql_references,
    looks_like_sql,
    split_callable_name,
)

_STATEMENT_TAGS = {"select", "insert", "update", "delete", "merge", "statement"}


class XmlSqlExtractor(BaseExtractor):
    def can_handle(self, file_path: str, text: str) -> bool:
        return Path(file_path).suffix.lower() == ".xml"

    def extract(
        self, file_path: str, text: str, context: ExtractionContext
    ) -> ExtractionResult:
        result = ExtractionResult(source_file=file_path, extractor_name=type(self).__name__)
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            sql = _fallback_text(text)
            if not looks_like_sql(sql):
                raise ValueError(f"Invalid XML: {file_path}") from exc
            _emit_sql(result, sql, file_path, context, query_id="")
            return result

        mapper_namespace = root.get("namespace", "").strip()
        fragments = {
            element.get("id", "").strip(): element
            for element in root.iter()
            if _local_name(element.tag).lower() == "sql" and element.get("id")
        }
        statements = [
            element
            for element in root.iter()
            if _local_name(element.tag).lower() in _STATEMENT_TAGS
        ]
        if statements:
            for element in statements:
                sql = _element_sql(element, fragments).strip()
                if looks_like_sql(sql):
                    statement_id = element.get("id", "").strip()
                    query_id = ".".join(
                        part for part in (mapper_namespace, statement_id) if part
                    )
                    _emit_sql(
                        result,
                        sql,
                        file_path,
                        context,
                        query_id=query_id,
                        mapper_tag=_local_name(element.tag).lower(),
                        line=_statement_line(text, element, sql),
                    )
        else:
            sql = " ".join(root.itertext()).strip()
            if looks_like_sql(sql):
                _emit_sql(result, sql, file_path, context, query_id="")
        return result


def _emit_sql(
    result: ExtractionResult,
    sql: str,
    file_path: str,
    context: ExtractionContext,
    *,
    query_id: str,
    mapper_tag: str = "",
    line: int = 1,
) -> None:
    references = extract_sql_references(sql)
    calls = extract_sql_calls(sql)
    if not references and not calls:
        return
    owner_qname = context.source_file_qname()
    source_path = normalize_repository_path(context.relative_source_path or Path(file_path).name)
    _add_node(
        result,
        GraphNode(
            S.LABEL_SOURCE_FILE,
            "qualified_name",
            owner_qname,
            {
                "qualified_name": owner_qname,
                "name": PurePosixPath(source_path).name,
                "repository": context.repository,
                "project": context.project_name,
                "source_id": context.source_id,
                "source_path": source_path,
                "layer": "logic",
            },
        ),
    )
    for reference in references:
        schema, name, unresolved = context.resolved_object(reference.object_name)
        table_qname = context.table_qname(reference.object_name)
        _add_node(
            result,
            GraphNode(
                S.LABEL_TABLE,
                "qualified_name",
                table_qname,
                {
                    "qualified_name": table_qname,
                    "name": name,
                    "schema": schema,
                    "db_name": context.db_name,
                    "repository": context.repository,
                    "unresolved": unresolved,
                    "layer": "data",
                },
            ),
        )
        result.edges.append(
            GraphEdge(
                S.LABEL_SOURCE_FILE,
                "qualified_name",
                owner_qname,
                S.LABEL_TABLE,
                "qualified_name",
                table_qname,
                reference.relation,
                {
                    "operation": reference.operation,
                    "query_id": query_id,
                    "mapper_tag": mapper_tag,
                    "line": line,
                    "source_file": file_path,
                },
            )
        )
    for call in calls:
        explicit_schema, callable_name = split_callable_name(call.object_name)
        target_qname = context.logic_qname(
            S.LABEL_PROCEDURE, callable_name, explicit_schema
        )
        _add_node(
            result,
            GraphNode(
                S.LABEL_PROCEDURE,
                "qualified_name",
                target_qname,
                {
                    "qualified_name": target_qname,
                    "name": callable_name.rsplit(".", 1)[-1],
                    "schema": explicit_schema or context.schema_name,
                    "repository": context.repository,
                    "db_name": context.db_name,
                    "external_reference": True,
                    "layer": "logic",
                },
            ),
        )
        result.edges.append(
            GraphEdge(
                S.LABEL_SOURCE_FILE,
                "qualified_name",
                owner_qname,
                S.LABEL_PROCEDURE,
                "qualified_name",
                target_qname,
                S.REL_CALLS,
                {
                    "operation": "CALL",
                    "query_id": query_id,
                    "mapper_tag": mapper_tag,
                    "line": line,
                    "source_file": file_path,
                },
            )
        )


def _element_sql(
    element: ElementTree.Element,
    fragments: dict[str, ElementTree.Element],
    active: frozenset[str] = frozenset(),
) -> str:
    parts = [element.text or ""]
    for child in element:
        if _local_name(child.tag).lower() == "include":
            refid = child.get("refid", "").strip()
            key = refid.rsplit(".", 1)[-1]
            fragment = fragments.get(refid)
            if fragment is None:
                fragment = fragments.get(key)
            if fragment is not None and key not in active:
                parts.append(_element_sql(fragment, fragments, active | {key}))
        else:
            parts.append(_element_sql(child, fragments, active))
        parts.append(child.tail or "")
    return " ".join(parts)


def _fallback_text(text: str) -> str:
    with_cdata = re.sub(
        r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL
    )
    return re.sub(r"<[^>]*>", " ", with_cdata)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _statement_line(text: str, element: ElementTree.Element, sql: str) -> int:
    statement_id = element.get("id", "").strip()
    if statement_id:
        match = re.search(
            rf"\bid\s*=\s*(['\"])" + re.escape(statement_id) + r"\1",
            text,
        )
        if match:
            return text.count("\n", 0, match.start()) + 1
    first_token = re.search(r"\S+", sql)
    if first_token:
        position = text.find(first_token.group(0))
        if position >= 0:
            return text.count("\n", 0, position) + 1
    return 1


def _add_node(result: ExtractionResult, node: GraphNode) -> None:
    if node.key_value not in {item.key_value for item in result.nodes}:
        result.nodes.append(node)
