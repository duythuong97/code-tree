from __future__ import annotations

import re
from dataclasses import dataclass

from code_tree_exporter.extractors.package_support.package_writer import (
    line_for_offset,
    line_text,
    slug,
    table_id,
)
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql

_CLASS_RE = re.compile(
    r"\b(?:public\s+|private\s+|internal\s+|sealed\s+|abstract\s+|static\s+|partial\s+)*class\s+([A-Za-z_]\w*)"
)
_ROUTE_RE = re.compile(r"\[Route\(\s*\"([^\"]+)\"\s*\)\]")
_HTTP_RE = re.compile(
    r"\[Http(Get|Post|Put|Patch|Delete)(?:\(\s*\"([^\"]*)\"\s*\))?\]", re.IGNORECASE
)
_METHOD_RE = re.compile(
    r"\b(?:public|private|internal)\s+(?:async\s+)?(?:[A-Za-z_][\w<>?,\s]*\s+)+([A-Za-z_]\w*)\s*\("
)
_PROC_NAME_RE = re.compile(r"^[A-Za-z_][\w$#]*(?:\.[A-Za-z_][\w$#]*){1,2}$")


@dataclass(frozen=True)
class ClassInfo:
    name: str
    start: int
    end: int


@dataclass(frozen=True)
class EndpointInfo:
    method: str
    route: str
    controller: str
    action: str
    offset: int


@dataclass(frozen=True)
class LiteralInfo:
    value: str
    start: int
    end: int
    owner_class: str


def class_spans(text: str) -> list[ClassInfo]:
    matches = list(_CLASS_RE.finditer(text))
    spans: list[ClassInfo] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        spans.append(ClassInfo(match.group(1), match.start(), end))
    return spans


def owner_class(classes: list[ClassInfo], offset: int) -> str:
    for item in classes:
        if item.start <= offset < item.end:
            return item.name
    return "UnknownClass"


def endpoints(text: str) -> list[EndpointInfo]:
    classes = class_spans(text)
    found: list[EndpointInfo] = []
    for cls in classes:
        if not cls.name.endswith("Controller"):
            continue
        block = text[cls.start : cls.end]
        attribute_prefix = text[max(0, cls.start - 500) : cls.start]
        route_match = _ROUTE_RE.search(attribute_prefix) or _ROUTE_RE.search(block)
        base_route = (
            route_match.group(1)
            if route_match
            else cls.name.removesuffix("Controller").lower()
        )
        for http in _HTTP_RE.finditer(block):
            after = block[http.end() : http.end() + 500]
            method_match = _METHOD_RE.search(after)
            action = method_match.group(1) if method_match else http.group(1).lower()
            action_route = http.group(2) or ""
            combined = "/" + "/".join(
                part.strip("/")
                for part in (base_route, action_route)
                if part.strip("/")
            )
            found.append(
                EndpointInfo(
                    http.group(1).upper(),
                    combined,
                    cls.name,
                    action,
                    cls.start + http.start(),
                )
            )
    return found


def csharp_literals(text: str) -> list[LiteralInfo]:
    classes = class_spans(text)
    found: list[LiteralInfo] = []
    index = 0
    while index < len(text):
        if text[index] == '"' or (
            text[index] == "@" and index + 1 < len(text) and text[index + 1] == '"'
        ):
            verbatim = text[index] == "@"
            start_quote = index + 1 if verbatim else index
            end = start_quote + 1
            value_chars: list[str] = []
            while end < len(text):
                ch = text[end]
                if ch == '"':
                    if verbatim and end + 1 < len(text) and text[end + 1] == '"':
                        value_chars.append('"')
                        end += 2
                        continue
                    break
                if not verbatim and ch == "\\" and end + 1 < len(text):
                    value_chars.append(text[end + 1])
                    end += 2
                    continue
                value_chars.append(ch)
                end += 1
            if end < len(text):
                literal_start = index
                literal_end = end + 1
                found.append(
                    LiteralInfo(
                        "".join(value_chars),
                        literal_start,
                        literal_end,
                        owner_class(classes, literal_start),
                    )
                )
                index = literal_end
                continue
        index += 1
    return found


def is_stored_procedure_literal(text: str, literal: LiteralInfo) -> bool:
    if not _PROC_NAME_RE.fullmatch(literal.value.strip()):
        return False
    window = text[max(0, literal.start - 250) : min(len(text), literal.end + 250)]
    return "StoredProcedure" in window


def source_line(text: str, offset: int) -> tuple[int, str]:
    line = line_for_offset(text, offset)
    return line, line_text(text, line)


def referenced_tables_from_literal(value: str) -> list[tuple[str, str, str, int]]:
    analysis = analyze_sql(value)
    return [
        (ref.object_name, ref.operation, ref.edge_type, ref.start)
        for ref in analysis.tables
    ]


def display_from_identifier(value: str) -> str:
    return " ".join(
        part.capitalize()
        for part in re.split(r"[-_.]", slug(value).replace(".exe", ""))
        if part
    )
