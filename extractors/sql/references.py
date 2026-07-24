from __future__ import annotations

import re
from dataclasses import dataclass

from contract import schema as S

_IDENT = r'(?:"[^"]+"|\[[^\]]+\]|[A-Za-z_#$][\w$#]*)'
_OBJECT = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}(?:@[\w$#]+)?"

_PATTERNS = (
    (
        "INSERT",
        S.REL_INSERTS_INTO,
        re.compile(rf"\bINSERT\s+INTO\s+({_OBJECT})", re.IGNORECASE),
    ),
    (
        "UPDATE",
        S.REL_UPDATES,
        re.compile(
            rf"\bUPDATE\s+({_OBJECT})(?:\s+(?:AS\s+)?[\w$#]+)?\s+SET\b", re.IGNORECASE
        ),
    ),
    (
        "DELETE",
        S.REL_DELETES_FROM,
        re.compile(rf"\bDELETE\s+FROM\s+({_OBJECT})", re.IGNORECASE),
    ),
    (
        "MERGE",
        S.REL_MERGES_INTO,
        re.compile(rf"\bMERGE\s+INTO\s+({_OBJECT})", re.IGNORECASE),
    ),
    (
        "SELECT",
        S.REL_READS_FROM,
        re.compile(rf"\bFROM\s+({_OBJECT})(?![\w$#])(?!\s*[.(])", re.IGNORECASE),
    ),
    (
        "SELECT",
        S.REL_READS_FROM,
        re.compile(rf"\bJOIN\s+({_OBJECT})(?![\w$#])(?!\s*[.(])", re.IGNORECASE),
    ),
)
_CALL_RE = re.compile(
    rf"\b(?:EXEC(?:UTE)?|CALL)\s+(?!IMMEDIATE\b)({_OBJECT})", re.IGNORECASE
)
_DELETE_RE = _PATTERNS[2][2]
_FROM_END_RE = re.compile(
    r"\b(?:WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|CONNECT\s+BY|START\s+WITH|UNION|MINUS|INTERSECT)\b",
    re.IGNORECASE,
)
_INSERT_MULTI_RE = re.compile(
    r"\bINSERT\s+(?:ALL|FIRST)\b(?P<body>.*?)(?:;|$)", re.IGNORECASE | re.DOTALL
)
_INTO_RE = re.compile(rf"\bINTO\s+({_OBJECT})", re.IGNORECASE)
_CTE_RE = re.compile(rf"(?:\bWITH|,)\s*({_IDENT})\s+AS\s*\(", re.IGNORECASE)
_SKIP = {
    "DUAL",
    "SELECT",
    "TABLE",
    "WITH",
    "XMLTABLE",
}


@dataclass(frozen=True)
class SqlReference:
    object_name: str
    operation: str
    relation: str
    start: int


def extract_sql_references(sql: str) -> list[SqlReference]:
    scan = _mask_noncode(sql)
    ctes = {
        _normalize_object(match.group(1)).upper() for match in _CTE_RE.finditer(scan)
    }
    delete_targets = {match.span(1) for match in _DELETE_RE.finditer(scan)}
    found: list[SqlReference] = []
    seen: set[tuple[str, str, int]] = set()
    for operation, relation, pattern in _PATTERNS:
        for match in pattern.finditer(scan):
            if relation == S.REL_READS_FROM and match.span(1) in delete_targets:
                continue
            _append_reference(
                found, seen, ctes, match.group(1), operation, relation, match.start(1)
            )
    for body_start, body in _from_clause_bodies(scan):
        for offset in _top_level_commas(body):
            match = re.match(
                rf"\s*({_OBJECT})(?![\w$#])(?!\s*[.(])",
                body[offset + 1 :],
                re.IGNORECASE,
            )
            if match:
                _append_reference(
                    found,
                    seen,
                    ctes,
                    match.group(1),
                    "SELECT",
                    S.REL_READS_FROM,
                    body_start + offset + 1 + match.start(1),
                )
    for statement in _INSERT_MULTI_RE.finditer(scan):
        for match in _INTO_RE.finditer(statement.group("body")):
            _append_reference(
                found,
                seen,
                ctes,
                match.group(1),
                "INSERT",
                S.REL_INSERTS_INTO,
                statement.start("body") + match.start(1),
            )
    return sorted(found, key=lambda item: (item.start, item.relation, item.object_name))


def _append_reference(
    found: list[SqlReference],
    seen: set[tuple[str, str, int]],
    ctes: set[str],
    raw_name: str,
    operation: str,
    relation: str,
    start: int,
) -> None:
    name = _normalize_object(raw_name)
    leaf = name.rsplit(".", 1)[-1].upper()
    if (
        not name
        or leaf in _SKIP
        or ("." not in name and leaf in ctes)
        or name.startswith("(")
    ):
        return
    key = (name, relation, start)
    if key not in seen:
        seen.add(key)
        found.append(SqlReference(name, operation, relation, start))


def _from_clause_bodies(text: str) -> list[tuple[int, str]]:
    clauses = []
    for match in re.finditer(r"\bFROM\b", text, re.IGNORECASE):
        start = match.end()
        depth = 0
        end = len(text)
        index = start
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    end = index
                    break
                depth -= 1
            elif depth == 0:
                if char == ";":
                    end = index
                    break
                terminator = _FROM_END_RE.match(text, index)
                if terminator:
                    end = index
                    break
            index += 1
        clauses.append((start, text[start:end]))
    return clauses


def _top_level_commas(text: str) -> list[int]:
    depth = 0
    commas = []
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            commas.append(index)
    return commas


def extract_sql_calls(sql: str) -> list[SqlReference]:
    scan = _mask_noncode(sql)
    return [
        SqlReference(
            _normalize_object(match.group(1)), "CALL", S.REL_CALLS, match.start()
        )
        for match in _CALL_RE.finditer(scan)
        if _normalize_object(match.group(1))
    ]


def split_callable_name(name: str) -> tuple[str | None, str]:
    parts = [part for part in _normalize_object(name).split(".") if part]
    if len(parts) >= 3:
        return parts[-3], ".".join(parts[-2:])
    return None, ".".join(parts[-2:])


def looks_like_sql(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:SELECT\b.+?\bFROM|INSERT\s+(?:(?:ALL|FIRST)\s+)?INTO|UPDATE\b.+?\bSET|DELETE\s+FROM|MERGE\s+INTO|EXEC(?:UTE)?\s+|CALL\s+)",
            _mask_noncode(text),
            re.IGNORECASE | re.DOTALL,
        )
    )


def _mask_noncode(text: str) -> str:
    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        pair = text[index : index + 2]
        if state == "code":
            if text[index] in "qQ" and index + 2 < len(text) and text[index + 1] == "'":
                opening = text[index + 2]
                closing = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opening, opening)
                end = text.find(closing + "'", index + 3)
                if end >= 0:
                    for offset in range(index, end + 2):
                        if chars[offset] != "\n":
                            chars[offset] = " "
                    index = end + 2
                    continue
            if pair == "--":
                state = "line_comment"
                chars[index : index + 2] = [" ", " "]
                index += 2
                continue
            if pair == "/*":
                state = "block_comment"
                chars[index : index + 2] = [" ", " "]
                index += 2
                continue
            if text[index] == "'":
                state = "string"
                chars[index] = " "
            index += 1
            continue
        if state == "line_comment":
            if text[index] == "\n":
                state = "code"
            else:
                chars[index] = " "
            index += 1
            continue
        if state == "block_comment":
            if pair == "*/":
                chars[index : index + 2] = [" ", " "]
                state = "code"
                index += 2
            else:
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            continue
        if text[index] == "'":
            chars[index] = " "
            if index + 1 < len(text) and text[index + 1] == "'":
                chars[index + 1] = " "
                index += 2
            else:
                state = "code"
                index += 1
        else:
            if text[index] != "\n":
                chars[index] = " "
            index += 1
    return "".join(chars)


def _normalize_object(name: str) -> str:
    name = re.sub(r"\s*@\s*", "@", name.strip())
    parts = []
    for raw in re.split(r"\s*\.\s*", name):
        part = raw.strip()
        if part.startswith("[") and part.endswith("]"):
            part = part[1:-1]
        parts.append(part)
    return ".".join(parts)
