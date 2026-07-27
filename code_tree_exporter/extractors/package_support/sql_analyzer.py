from __future__ import annotations

import re
from dataclasses import dataclass

from code_tree_exporter.extractors.package_support.oracle_parser import OraclePlsqlParser, OracleSqlParser
from code_tree_exporter.extractors.sql.references import extract_sql_calls, extract_sql_references, looks_like_sql

OP_TO_EDGE = {
    "SELECT": "READS",
    "INSERT": "INSERTS",
    "UPDATE": "UPDATES",
    "DELETE": "DELETES",
    "MERGE": "MERGES",
}

_REMOTE_RE = re.compile(r"@[A-Za-z_][\w$#]*")
_SEQUENCE_RE = re.compile(r"\b((?:[A-Za-z_][\w$#]*\.)?[A-Za-z_][\w$#]*)\s*\.\s*(?:NEXTVAL|CURRVAL)\b", re.IGNORECASE)
_EXECUTE_IMMEDIATE_RE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
_ASSIGN_RE = re.compile(r"\b([A-Za-z_][\w$#]*)\b[^;:=]*:=", re.IGNORECASE)
_MALFORMED_RE = re.compile(r"\bSELECT\s+FROM\s+WHERE\b", re.IGNORECASE)
_KEYWORDS = {"SELECT", "FROM", "WHERE", "JOIN", "ON", "OF", "SET", "VALUES", "INTO", "USING"}
_SYSTEM_OWNERS = {"SYS", "SYSTEM", "CTXSYS", "MDSYS", "ORDSYS", "OUTLN", "XDB"}
_SYSTEM_OBJECT_PREFIXES = ("ALL_", "USER_", "DBA_", "V_$", "GV_$", "V$", "GV$", "NLS_", "SESSION_")
_SYSTEM_OBJECTS = {"DUAL", "PLAN_TABLE", "SQLERRM", "SQLCODE", "DBMS_OUTPUT", "DBMS_UTILITY", "DBMS_SQL", "UTL_HTTP", "UTL_FILE"}

@dataclass(frozen=True)
class TableReference:
    object_name: str
    operation: str
    edge_type: str
    start: int
    remote: bool = False

@dataclass(frozen=True)
class CallReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class SequenceReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class SqlAnalysis:
    tables: list[TableReference]
    calls: list[CallReference]
    sequences: list[SequenceReference]
    dynamic_offsets: list[int]
    parse_error_offsets: list[int]


def analyze_sql(text: str) -> SqlAnalysis:
    parser = OracleSqlParser(text)
    antlr_parser = OraclePlsqlParser(text)
    parsed_tables = parser.table_references()
    if parsed_tables:
        table_refs = [
            TableReference(ref.object_name, ref.operation, ref.relation, ref.start, bool(_REMOTE_RE.search(ref.object_name)))
            for ref in parsed_tables
            if not _should_skip_reference(ref.object_name)
        ]
    else:
        table_refs = []
        if looks_like_sql(text):
            for ref in extract_sql_references(text):
                if _should_skip_reference(ref.object_name):
                    continue
                edge_type = OP_TO_EDGE.get(ref.operation, "READS")
                remote = bool(_REMOTE_RE.search(ref.object_name))
                if remote and edge_type == "READS":
                    edge_type = "REMOTE_READS"
                table_refs.append(TableReference(ref.object_name, ref.operation, edge_type, ref.start, remote))
    dynamic_tables, dynamic_sequences, unresolved_dynamic = _dynamic_sql_references(text)
    table_refs.extend(dynamic_tables)
    parsed_calls = parser.calls()
    call_refs = {(ref.start, ref.object_name.upper()): CallReference(ref.object_name, ref.start) for ref in parsed_calls}
    for ref in antlr_parser.calls():
        if "." in ref.object_name:
            call_refs.setdefault((ref.start, ref.object_name.upper()), CallReference(ref.object_name, ref.start))
    if not call_refs:
        call_refs = {
            (ref.start, ref.object_name.upper()): CallReference(ref.object_name, ref.start)
            for ref in extract_sql_calls(text)
        }
    calls = sorted(call_refs.values(), key=lambda item: (item.start, item.object_name))
    sequences = [SequenceReference(ref.object_name, ref.start) for ref in parser.sequences()]
    if not sequences:
        sequences = [SequenceReference(match.group(1), match.start(1)) for match in _SEQUENCE_RE.finditer(text)]
    sequences.extend(dynamic_sequences)
    dynamic = unresolved_dynamic
    explicit_call_starts = {ref.start for ref in parsed_calls}
    antlr_error_offsets = {
        _offset_for_line_column(text, line, column)
        for line, column, _ in antlr_parser.syntax_errors
    } - explicit_call_starts
    parse_errors = _first_offset_per_line(text, {
        *parser.parse_error_offsets(),
        *(match.start() for match in _MALFORMED_RE.finditer(text)),
        *antlr_error_offsets,
    })
    return SqlAnalysis(table_refs, calls, sequences, dynamic, parse_errors)


def routine_signature(parameter_block: str | None) -> str:
    if not parameter_block:
        return "void"
    body = parameter_block.strip()[1:-1]
    types: list[str] = []
    for raw_part in body.split(","):
        part = raw_part.strip()
        if not part:
            continue
        tokens = re.sub(r"\b(?:IN|OUT|IN\s+OUT|NOCOPY|DEFAULT)\b", " ", part, flags=re.IGNORECASE).split()
        if len(tokens) >= 2:
            types.append(tokens[1].upper())
    return "_".join(types) if types else "void"

def _dynamic_sql_references(text: str) -> tuple[list[TableReference], list[SequenceReference], list[int]]:
    table_refs: list[TableReference] = []
    sequences: list[SequenceReference] = []
    unresolved: list[int] = []
    for start, expression in _execute_immediate_expressions(text):
        variables = _literal_assignments_before(text, start)
        sql_text = _resolve_literal_expression(expression, variables)
        if not sql_text:
            unresolved.append(start)
            continue
        parser = OracleSqlParser(sql_text)
        for ref in parser.table_references():
            if _should_skip_reference(ref.object_name):
                continue
            remote = bool(_REMOTE_RE.search(ref.object_name))
            edge_type = "REMOTE_READS" if remote and ref.relation == "READS" else ref.relation
            table_refs.append(TableReference(ref.object_name, ref.operation, edge_type, start, remote))
        for seq in parser.sequences():
            sequences.append(SequenceReference(seq.object_name, start))
    return table_refs, sequences, unresolved

def _execute_immediate_expressions(text: str) -> list[tuple[int, str]]:
    expressions: list[tuple[int, str]] = []
    scan = _mask_comments(text)
    for match in _EXECUTE_IMMEDIATE_RE.finditer(scan):
        expression_start = match.end()
        expression_end = _expression_end(text, expression_start, stop_keywords={"USING", "RETURNING", "INTO"})
        expressions.append((match.start(), text[expression_start:expression_end].strip()))
    return expressions

def _literal_assignments_before(text: str, before_offset: int) -> dict[str, str]:
    values: dict[str, str] = {}
    scan = _mask_comments(text[:before_offset])
    for match in _ASSIGN_RE.finditer(scan):
        expression_start = match.end()
        expression_end = _expression_end(text, expression_start, limit=before_offset)
        resolved = _resolve_literal_expression(text[expression_start:expression_end], values)
        if resolved is not None:
            values[match.group(1).upper()] = resolved
    return values

def _expression_end(text: str, start: int, *, limit: int | None = None, stop_keywords: set[str] | None = None) -> int:
    stop_keywords = stop_keywords or set()
    end_limit = len(text) if limit is None else min(limit, len(text))
    index = start
    while index < end_limit:
        literal_end = _literal_end(text, index)
        if literal_end is not None:
            index = literal_end
            continue
        if text[index] == ";":
            return index
        if stop_keywords and (index == 0 or not _is_ident_char(text[index - 1])):
            keyword = re.match(r"[A-Za-z_][\w$#]*", text[index:])
            if keyword and keyword.group(0).upper() in stop_keywords:
                return index
        index += 1
    return end_limit

def _resolve_literal_expression(expression: str, variables: dict[str, str]) -> str | None:
    pieces: list[str] = []
    index = 0
    while index < len(expression):
        char = expression[index]
        if char.isspace() or char in "()":
            index += 1
            continue
        if expression.startswith("||", index):
            index += 2
            continue
        literal = _read_literal(expression, index)
        if literal is not None:
            value, index = literal
            pieces.append(value)
            continue
        ident = re.match(r"[A-Za-z_][\w$#]*", expression[index:])
        if ident:
            name = ident.group(0).upper()
            if name not in variables:
                return None
            pieces.append(variables[name])
            index += len(ident.group(0))
            continue
        return None
    return "".join(pieces) if pieces else None

def _read_literal(text: str, index: int) -> tuple[str, int] | None:
    if index + 1 < len(text) and text[index].upper() == "N" and text[index + 1] == "'":
        index += 1
    if index + 2 < len(text) and text[index].upper() == "Q" and text[index + 1] == "'":
        opening = text[index + 2]
        closing = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opening, opening)
        end = text.find(closing + "'", index + 3)
        if end < 0:
            return None
        return text[index + 3:end], end + 2
    if index >= len(text) or text[index] != "'":
        return None
    pieces: list[str] = []
    cursor = index + 1
    while cursor < len(text):
        if text[cursor] == "'":
            if cursor + 1 < len(text) and text[cursor + 1] == "'":
                pieces.append("'")
                cursor += 2
                continue
            return "".join(pieces), cursor + 1
        pieces.append(text[cursor])
        cursor += 1
    return None

def _literal_end(text: str, index: int) -> int | None:
    literal = _read_literal(text, index)
    if literal is not None:
        return literal[1]
    return None

def _mask_comments(text: str) -> str:
    chars = list(text)
    index = 0
    while index < len(chars):
        pair = text[index:index + 2]
        if pair == "--":
            end = text.find("\n", index + 2)
            end = len(text) if end < 0 else end
            for offset in range(index, end):
                chars[offset] = " "
            index = end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
            for offset in range(index, end):
                if chars[offset] != "\n":
                    chars[offset] = " "
            index = end
            continue
        index += 1
    return "".join(chars)

def _should_skip_reference(name: str) -> bool:
    upper = name.upper()
    if upper in _KEYWORDS:
        return True
    clean = upper.split("@", 1)[0]
    parts = [part.strip('"[]') for part in clean.split(".") if part]
    if not parts:
        return True
    leaf = parts[-1]
    owner = parts[-2] if len(parts) >= 2 else ""
    return (
        leaf in _KEYWORDS
        or leaf in _SYSTEM_OBJECTS
        or owner in _SYSTEM_OWNERS
        or any(leaf.startswith(prefix) for prefix in _SYSTEM_OBJECT_PREFIXES)
    )

def _offset_for_line_column(text: str, line: int, column: int) -> int:
    lines = text.splitlines(keepends=True)
    if line <= 0:
        return 0
    return min(sum(len(item) for item in lines[: line - 1]) + max(column, 0), len(text))

def _first_offset_per_line(text: str, offsets: set[int]) -> list[int]:
    first: dict[int, int] = {}
    for offset in sorted(offsets):
        line = text.count("\n", 0, offset)
        first.setdefault(line, offset)
    return list(first.values())

def _is_ident_char(char: str) -> bool:
    return char.isalnum() or char in {"_", "$", "#"}
