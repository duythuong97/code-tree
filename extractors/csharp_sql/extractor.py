from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from contract import schema as S
from contract.entities import ExtractionContext, ExtractionResult, GraphEdge, GraphNode
from extractors.base import BaseExtractor
from extractors.sql.references import (
    extract_sql_calls,
    extract_sql_references,
    looks_like_sql,
    split_callable_name,
)

_NAMESPACE_RE = re.compile(r"\bnamespace\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)")
_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
_STORED_PROCEDURE = re.compile(r"CommandType\s*\.\s*StoredProcedure", re.IGNORECASE)
_PROC_NAME = re.compile(r"^[A-Za-z_#$][\w$#]*(?:\.[A-Za-z_#$][\w$#]*){0,2}$")


@dataclass(frozen=True)
class _Literal:
    start: int
    end: int
    value: str


@dataclass(frozen=True)
class _ClassSpan:
    start: int
    end: int
    name: str


@dataclass(frozen=True)
class _NamespaceSpan:
    start: int
    end: int
    name: str


class CSharpSqlExtractor(BaseExtractor):
    def can_handle(self, file_path: str, text: str) -> bool:
        return Path(file_path).suffix.lower() == ".cs"

    def extract(
        self, file_path: str, text: str, context: ExtractionContext
    ) -> ExtractionResult:
        result = ExtractionResult(
            source_file=file_path, extractor_name=type(self).__name__
        )
        namespaces = _namespace_spans(text)
        classes = _class_spans(text)
        for literal in _composed_literals(text):
            namespace = _namespace_at(namespaces, literal.start)
            references = (
                extract_sql_references(literal.value)
                if looks_like_sql(literal.value)
                else []
            )
            calls = (
                extract_sql_calls(literal.value)
                if looks_like_sql(literal.value)
                else []
            )
            if _is_stored_procedure_literal(text, literal):
                calls = [_stored_procedure_reference(literal.value)]
            unresolved_sql = _is_unresolved_sql(text, literal)
            if unresolved_sql:
                references = []
                calls = []
            if not references and not calls and not unresolved_sql:
                continue
            owner_label, owner_qname, owner_name = _owner(
                context, namespace, classes, literal.start
            )
            _add_owner(
                result,
                owner_label,
                owner_qname,
                owner_name,
                namespace,
                file_path,
                context,
                unresolved_sql,
            )
            line = text.count("\n", 0, literal.start) + 1
            for reference in references:
                schema, name, unresolved = context.resolved_object(
                    reference.object_name
                )
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
                        owner_label,
                        "qualified_name",
                        owner_qname,
                        S.LABEL_TABLE,
                        "qualified_name",
                        table_qname,
                        reference.relation,
                        {
                            "operation": reference.operation,
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
                        owner_label,
                        "qualified_name",
                        owner_qname,
                        S.LABEL_PROCEDURE,
                        "qualified_name",
                        target_qname,
                        S.REL_CALLS,
                        {
                            "operation": "CALL",
                            "call_type": "StoredProcedure",
                            "line": line,
                            "source_file": file_path,
                        },
                    )
                )
        return result


def _stored_procedure_reference(name: str):
    from extractors.sql.references import SqlReference

    return SqlReference(name.strip(), "CALL", S.REL_CALLS, 0)


def _is_unresolved_sql(text: str, literal: _Literal) -> bool:
    if not looks_like_sql(literal.value):
        return False
    source = text[literal.start : literal.end]
    quote = source.find('"')
    if quote >= 0 and "$" in source[: quote + 1] and "{" in literal.value:
        return True
    statement_start = (
        max(
            text.rfind(";", 0, literal.start),
            text.rfind("{", 0, literal.start),
            text.rfind("}", 0, literal.start),
        )
        + 1
    )
    statement_end = text.find(";", literal.end)
    if statement_end < 0:
        statement_end = len(text)
    before = text[statement_start : literal.start]
    after = text[literal.end : statement_end]
    return bool(
        re.search(r"(?:[A-Za-z_]\w*|[.)\]])\s*\+\s*$", before, re.DOTALL)
        or re.match(r"\s*\+\s*(?![$@]*\")", after, re.DOTALL)
        or re.search(
            r"\b(?:string\s*\.\s*)?(?:Format|Concat)\s*\([^)]*$",
            before,
            re.IGNORECASE | re.DOTALL,
        )
    )


def _is_stored_procedure_literal(text: str, literal: _Literal) -> bool:
    if not _PROC_NAME.fullmatch(literal.value.strip()):
        return False
    masked = _mask_comments_and_strings(text)
    if _stored_procedure_call(masked, literal.start):
        return True

    before = masked[: literal.start]
    assignment = re.search(
        r"(?P<receiver>[A-Za-z_]\w*)\.CommandText\s*=\s*$",
        before,
        re.IGNORECASE,
    )
    block_start, block_end = _enclosing_braces(masked, literal.start)
    block = masked[block_start:block_end]
    if assignment:
        receiver = assignment.group("receiver")
        local_position = literal.start - block_start
        assignments = [
            match.start()
            for match in re.finditer(
                rf"\b{re.escape(receiver)}\.CommandText\s*=", block, re.IGNORECASE
            )
        ]
        current = max(
            (index for index, item in enumerate(assignments) if item < local_position),
            default=0,
        )
        previous = assignments[current - 1] if current > 0 else 0
        following = (
            assignments[current + 1] if current + 1 < len(assignments) else len(block)
        )
        lifecycle = block[previous:following]
        command_types = list(
            re.finditer(
                rf"\b{re.escape(receiver)}\.CommandType\s*=\s*"
                r"CommandType\.(?P<type>[A-Za-z_]\w*)",
                lifecycle,
                re.IGNORECASE,
            )
        )
        if command_types:
            return command_types[-1].group("type").lower() == "storedprocedure"

    initializer = re.search(r"\bCommandText\s*=\s*$", before)
    if initializer and re.search(
        rf"\bCommandType\s*=\s*{_STORED_PROCEDURE.pattern}",
        block,
        re.IGNORECASE,
    ):
        prefix = masked[max(0, block_start - 120) : block_start]
        return bool(re.search(r"\bnew\s+[A-Za-z_]\w*Command\s*$", prefix))
    return False


def _stored_procedure_call(masked: str, position: int) -> bool:
    stack: list[int] = []
    for index, char in enumerate(masked[:position]):
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            stack.pop()
    for opening in reversed(stack):
        closing = _matching_delimiter(masked, opening, "(", ")")
        if closing < position:
            continue
        prefix = masked[max(0, opening - 180) : opening]
        call = re.search(
            r"(?P<name>Query\w*|Execute\w*|CommandDefinition|[A-Za-z_]\w*Command)"
            r"\s*(?:<[^>]{0,120}>)?\s*$",
            prefix,
            re.IGNORECASE | re.DOTALL,
        )
        if not call or not _is_command_argument(masked, opening, position):
            continue
        if _STORED_PROCEDURE.search(masked[opening:closing]):
            return True
        if call.group("name").lower().endswith("command"):
            initializer = re.match(r"\s*\{", masked[closing:])
            if initializer:
                brace = closing + initializer.end() - 1
                end = _matching_delimiter(masked, brace, "{", "}")
                if _STORED_PROCEDURE.search(masked[brace:end]):
                    return True
    return False


def _is_command_argument(text: str, opening: int, position: int) -> bool:
    depth = 0
    argument_start = opening + 1
    argument_index = 0
    for index in range(opening + 1, position):
        if text[index] in "([{":
            depth += 1
        elif text[index] in ")]}" and depth:
            depth -= 1
        elif text[index] == "," and depth == 0:
            argument_start = index + 1
            argument_index += 1
    prefix = text[argument_start:position].strip()
    return argument_index == 0 or bool(
        re.fullmatch(r"(?:sql|commandText)\s*:\s*", prefix, re.IGNORECASE)
    )


def _enclosing_braces(text: str, position: int) -> tuple[int, int]:
    stack: list[int] = []
    for index, char in enumerate(text[:position]):
        if char == "{":
            stack.append(index)
        elif char == "}" and stack:
            stack.pop()
    if not stack:
        return 0, len(text)
    opening = stack[-1]
    return opening, _matching_delimiter(text, opening, "{", "}")


def _matching_delimiter(text: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def _owner(
    context: ExtractionContext,
    namespace: str,
    classes: list[_ClassSpan],
    position: int,
) -> tuple[str, str, str]:
    active = [span for span in classes if span.start <= position <= span.end]
    class_name = max(active, key=lambda span: span.start).name if active else ""
    if class_name.upper().endswith(("REPOSITORY", "DAO")):
        return (
            S.LABEL_REPOSITORY,
            context.repository_owner_qname(namespace, class_name),
            class_name,
        )
    return (
        S.LABEL_APPLICATION,
        context.application_qname(),
        context.project_name or context.repository,
    )


def _add_owner(
    result: ExtractionResult,
    label: str,
    qname: str,
    name: str,
    namespace: str,
    file_path: str,
    context: ExtractionContext,
    unresolved_sql: bool = False,
) -> None:
    properties = {
        "qualified_name": qname,
        "name": name,
        "namespace": namespace,
        "repository": context.repository,
        "project": context.project_name or context.repository,
        "source_file": file_path,
        "layer": "logic",
    }
    if unresolved_sql:
        properties.update({"unresolved": True, "unresolved_sql": True})
    _add_node(
        result,
        GraphNode(label, "qualified_name", qname, properties),
    )


def _namespace_spans(text: str) -> list[_NamespaceSpan]:
    masked = _mask_comments_and_strings(text)
    spans = []
    for match in _NAMESPACE_RE.finditer(masked):
        cursor = match.end()
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        end = (
            _matching_brace(masked, cursor)
            if cursor < len(masked) and masked[cursor] == "{"
            else len(masked)
        )
        spans.append(_NamespaceSpan(match.start(), end, match.group(1)))
    return spans


def _namespace_at(spans: list[_NamespaceSpan], position: int) -> str:
    active = sorted(
        (span for span in spans if span.start <= position <= span.end),
        key=lambda span: span.start,
    )
    return ".".join(span.name for span in active)


def _class_spans(text: str) -> list[_ClassSpan]:
    masked = _mask_comments_and_strings(text)
    spans = []
    for match in _CLASS_RE.finditer(masked):
        brace = masked.find("{", match.end())
        if brace < 0:
            continue
        spans.append(
            _ClassSpan(match.start(), _matching_brace(masked, brace), match.group(1))
        )
    return spans


def _matching_brace(text: str, start: int) -> int:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _composed_literals(text: str) -> list[_Literal]:
    literals = _string_literals(text)
    if not literals:
        return []
    composed = [literals[0]]
    for literal in literals[1:]:
        previous = composed[-1]
        if re.fullmatch(r"\s*\+\s*", text[previous.end : literal.start]):
            composed[-1] = _Literal(
                previous.start, literal.end, previous.value + literal.value
            )
        else:
            composed.append(literal)
    return composed


def _string_literals(text: str) -> list[_Literal]:
    literals: list[_Literal] = []
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        if text[index] == "'":
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                elif text[index] == "'":
                    index += 1
                    break
                else:
                    index += 1
            continue

        start = index
        cursor = index
        while cursor < len(text) and text[cursor] == "$":
            cursor += 1
        quote_count = 0
        while cursor + quote_count < len(text) and text[cursor + quote_count] == '"':
            quote_count += 1
        if quote_count >= 3:
            delimiter = '"' * quote_count
            body_start = cursor + quote_count
            end = text.find(delimiter, body_start)
            if end < 0:
                break
            literals.append(_Literal(start, end + quote_count, text[body_start:end]))
            index = end + quote_count
            continue

        prefix = ""
        for candidate in ("$@", "@$", "@", "$"):
            if text.startswith(candidate + '"', index):
                prefix = candidate
                cursor = index + len(candidate)
                break
        else:
            if text[index] != '"':
                index += 1
                continue
            cursor = index

        verbatim = "@" in prefix
        cursor += 1
        value: list[str] = []
        while cursor < len(text):
            char = text[cursor]
            if char == '"':
                if verbatim and cursor + 1 < len(text) and text[cursor + 1] == '"':
                    value.append('"')
                    cursor += 2
                    continue
                literals.append(_Literal(start, cursor + 1, "".join(value)))
                cursor += 1
                break
            if not verbatim and char == "\\" and cursor + 1 < len(text):
                escaped = text[cursor + 1]
                value.append({"n": "\n", "r": "\r", "t": "\t"}.get(escaped, escaped))
                cursor += 2
                continue
            value.append(char)
            cursor += 1
        index = max(cursor, index + 1)
    return literals


def _mask_comments_and_strings(text: str) -> str:
    chars = list(text)
    ranges = [(item.start, item.end) for item in _string_literals(text)]
    index = 0
    while index < len(text):
        if text.startswith("//", index):
            end = text.find("\n", index + 2)
            ranges.append((index, len(text) if end < 0 else end))
            index = len(text) if end < 0 else end
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            ranges.append((index, len(text) if end < 0 else end + 2))
            index = len(text) if end < 0 else end + 2
        elif any(start == index for start, _ in ranges):
            index = next(end for start, end in ranges if start == index)
        else:
            index += 1
    for start, end in ranges:
        for offset in range(start, end):
            if chars[offset] != "\n":
                chars[offset] = " "
    return "".join(chars)


def _add_node(result: ExtractionResult, node: GraphNode) -> None:
    for existing in result.nodes:
        if existing.key_value == node.key_value:
            existing.properties.update(node.properties)
            return
    result.nodes.append(node)
