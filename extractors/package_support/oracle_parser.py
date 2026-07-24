from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from contract import schema as S

OP_TO_EDGE = {
    "SELECT": "READS",
    "INSERT": "INSERTS",
    "UPDATE": "UPDATES",
    "DELETE": "DELETES",
    "MERGE": "MERGES",
}

_OBJECT_END_KEYWORDS = {
    "WHERE",
    "GROUP",
    "ORDER",
    "HAVING",
    "CONNECT",
    "START",
    "UNION",
    "MINUS",
    "INTERSECT",
    "WHEN",
    "ON",
    "SET",
    "VALUES",
    "RETURNING",
    "INTO",
    "USING",
    "PIVOT",
    "UNPIVOT",
    "MODEL",
}
_SKIP_OBJECTS = {"DUAL", "SELECT", "TABLE", "WITH", "XMLTABLE", "JSON_TABLE", "WHERE"}
_SYSTEM_OWNERS = {"SYS", "SYSTEM", "CTXSYS", "MDSYS", "ORDSYS", "OUTLN", "XDB"}
_SYSTEM_OBJECT_PREFIXES = ("ALL_", "USER_", "DBA_", "V_$", "GV_$", "V$", "GV$", "NLS_", "SESSION_")
_SYSTEM_OBJECTS = {
    "PLAN_TABLE",
    "SQLERRM",
    "SQLCODE",
    "DBMS_OUTPUT",
    "DBMS_UTILITY",
    "DBMS_SQL",
    "UTL_HTTP",
    "UTL_FILE",
}
_FROM_TERMINATORS = _OBJECT_END_KEYWORDS | {"JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "FULL", "CROSS"}
_DML_STARTERS = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"}
_NON_CALL_PREFIXES = {"EXECUTE", "EXEC", "CALL"}

@dataclass(frozen=True)
class Token:
    text: str
    upper: str
    start: int
    end: int
    kind: str

@dataclass(frozen=True)
class ParsedSqlReference:
    object_name: str
    operation: str
    relation: str
    start: int

@dataclass(frozen=True)
class ParsedCallReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class ParsedSequenceReference:
    object_name: str
    start: int

@dataclass(frozen=True)
class ParsedRoutineDeclaration:
    kind: str
    name: str
    parameter_block: str | None
    start: int
    end: int

@dataclass(frozen=True)
class ParsedTriggerDeclaration:
    name: str
    table_name: str
    start: int
    end: int

@dataclass(frozen=True)
class ParsedSynonymDeclaration:
    name: str
    target_name: str
    start: int

class OracleSqlParser:
    """Deterministic token-walking Oracle SQL parser.

    References derive from token positions and statement context; comments and
    strings remain excluded from source parsing.
    """

    def __init__(self, text: str):
        self.text = text
        self.tokens = list(_tokenize(text))

    def table_references(self) -> list[ParsedSqlReference]:
        refs: list[ParsedSqlReference] = []
        seen: set[tuple[str, str, int]] = set()
        ctes = self._cte_names()
        index = 0
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.upper == "INSERT":
                index = self._parse_insert(index, refs, seen, ctes)
                continue
            if token.upper == "UPDATE":
                parsed = self._parse_object(index + 1)
                if parsed:
                    name, start, next_index = parsed
                    self._append(refs, seen, ctes, name, "UPDATE", OP_TO_EDGE["UPDATE"], start)
                    index = next_index
                    continue
            if token.upper == "DELETE" and self._upper(index + 1) == "FROM":
                parsed = self._parse_object(index + 2)
                if parsed:
                    name, start, next_index = parsed
                    self._append(refs, seen, ctes, name, "DELETE", OP_TO_EDGE["DELETE"], start)
                    index = next_index
                    continue
            if token.upper == "MERGE" and self._upper(index + 1) == "INTO":
                parsed = self._parse_object(index + 2)
                if parsed:
                    name, start, next_index = parsed
                    self._append(refs, seen, ctes, name, "MERGE", OP_TO_EDGE["MERGE"], start)
                    index = next_index
                    continue
            if token.upper in {"FROM", "JOIN"}:
                index = self._parse_from_item(index + 1, refs, seen, ctes)
                continue
            if token.upper == "USING" and self._previous_dml(index) == "MERGE":
                index = self._parse_from_item(index + 1, refs, seen, ctes)
                continue
            index += 1
        return sorted(refs, key=lambda item: (item.start, item.relation, item.object_name))

    def calls(self) -> list[ParsedCallReference]:
        calls: list[ParsedCallReference] = []
        for index, token in enumerate(self.tokens):
            if token.upper not in _NON_CALL_PREFIXES or self._upper(index + 1) == "IMMEDIATE":
                continue
            parsed = self._parse_object(index + 1)
            if parsed:
                name, start, _ = parsed
                calls.append(ParsedCallReference(name, start))
        return calls

    def sequences(self) -> list[ParsedSequenceReference]:
        refs: list[ParsedSequenceReference] = []
        for index, token in enumerate(self.tokens):
            if token.upper not in {"NEXTVAL", "CURRVAL"}:
                continue
            if self._text(index - 1) != "." or self._kind(index - 2) != "IDENT":
                continue
            cursor = index - 2
            ordered: list[Token] = [self.tokens[cursor]]
            while self._text(cursor - 1) == "." and self._kind(cursor - 2) == "IDENT":
                cursor -= 2
                ordered.insert(0, self.tokens[cursor])
            refs.append(ParsedSequenceReference(".".join(part.text.strip('"[]') for part in ordered), ordered[0].start))
        return refs

    def dynamic_offsets(self) -> list[int]:
        return [
            token.start
            for index, token in enumerate(self.tokens)
            if token.upper == "EXECUTE" and self._upper(index + 1) == "IMMEDIATE" and self._kind(index + 2) == "IDENT"
        ]

    def parse_error_offsets(self) -> list[int]:
        offsets: list[int] = []
        for index, token in enumerate(self.tokens):
            if token.upper == "SELECT" and self._upper(index + 1) == "FROM" and self._upper(index + 2) == "WHERE":
                offsets.append(token.start)
        return offsets

    def _parse_insert(self, index: int, refs: list[ParsedSqlReference], seen: set[tuple[str, str, int]], ctes: set[str]) -> int:
        cursor = index + 1
        if self._upper(cursor) in {"ALL", "FIRST"}:
            while cursor < len(self.tokens) and self.tokens[cursor].text != ";":
                if self._upper(cursor) == "INTO":
                    parsed = self._parse_object(cursor + 1)
                    if parsed:
                        name, start, next_index = parsed
                        self._append(refs, seen, ctes, name, "INSERT", OP_TO_EDGE["INSERT"], start)
                        cursor = next_index
                        continue
                cursor += 1
            return cursor
        if self._upper(cursor) == "INTO":
            cursor += 1
        parsed = self._parse_object(cursor)
        if parsed:
            name, start, next_index = parsed
            self._append(refs, seen, ctes, name, "INSERT", OP_TO_EDGE["INSERT"], start)
            return next_index
        return index + 1

    def _parse_from_item(self, index: int, refs: list[ParsedSqlReference], seen: set[tuple[str, str, int]], ctes: set[str]) -> int:
        if self._text(index) == "(":
            return index + 1
        if self._upper(index) in {"TABLE", "XMLTABLE", "JSON_TABLE"} and self._text(index + 1) == "(":
            return index + 2
        cursor = index
        while cursor < len(self.tokens):
            if self._upper(cursor) in _FROM_TERMINATORS or self.tokens[cursor].text in {";", ")"}:
                break
            if self.tokens[cursor].text == "(":
                return cursor + 1
            if self._upper(cursor) in {"TABLE", "XMLTABLE", "JSON_TABLE"} and self._text(cursor + 1) == "(":
                return cursor + 2
            if self.tokens[cursor].text == ",":
                cursor += 1
                continue
            parsed = self._parse_object(cursor)
            if not parsed:
                cursor += 1
                continue
            name, start, next_index = parsed
            if self._text(next_index) != "(":
                self._append(refs, seen, ctes, name, "SELECT", OP_TO_EDGE["SELECT"], start)
            cursor = next_index
            # Skip simple aliases after a FROM/JOIN object.
            if self._kind(cursor) == "IDENT" and self._upper(cursor) not in _FROM_TERMINATORS:
                cursor += 1
            if self._text(cursor) != ",":
                break
        return max(cursor, index + 1)

    def _parse_object(self, index: int) -> tuple[str, int, int] | None:
        cursor = index
        parts: list[str] = []
        start = -1
        while cursor < len(self.tokens):
            token = self.tokens[cursor]
            if token.kind != "IDENT":
                break
            if token.upper in _OBJECT_END_KEYWORDS or (not parts and token.upper in _DML_STARTERS):
                return None
            if start < 0:
                start = token.start
            parts.append(token.text.strip('"[]'))
            cursor += 1
            if self._text(cursor) == "." and self._kind(cursor + 1) == "IDENT":
                cursor += 1
                continue
            break
        if not parts:
            return None
        if self._text(cursor) == "@" and self._kind(cursor + 1) == "IDENT":
            parts[-1] = parts[-1] + "@" + self.tokens[cursor + 1].text.strip('"[]')
            cursor += 2
        return ".".join(parts), start, cursor

    def _append(self, refs: list[ParsedSqlReference], seen: set[tuple[str, str, int]], ctes: set[str], name: str, operation: str, relation: str, start: int) -> None:
        normalized = _normalize_object(name)
        leaf = normalized.split("@", 1)[0].rsplit(".", 1)[-1].upper()
        if not normalized or leaf in _SKIP_OBJECTS or _is_system_object(normalized) or ("." not in normalized and leaf in ctes):
            return
        edge = "REMOTE_READS" if "@" in normalized and relation == OP_TO_EDGE["SELECT"] else relation
        key = (normalized, edge, start)
        if key in seen:
            return
        seen.add(key)
        refs.append(ParsedSqlReference(normalized, operation, edge, start))

    def _cte_names(self) -> set[str]:
        names: set[str] = set()
        for index, token in enumerate(self.tokens):
            if token.upper not in {"WITH", ","} or self._kind(index + 1) != "IDENT":
                continue
            cursor = index + 2
            if self._text(cursor) == "(":
                cursor = self._matching_paren(cursor) + 1
            if self._upper(cursor) == "AS" and self._text(cursor + 1) == "(":
                names.add(self.tokens[index + 1].upper)
        return names

    def _matching_paren(self, index: int) -> int:
        depth = 0
        cursor = index
        while cursor < len(self.tokens):
            if self._text(cursor) == "(":
                depth += 1
            elif self._text(cursor) == ")":
                depth -= 1
                if depth == 0:
                    return cursor
            cursor += 1
        return index

    def _previous_dml(self, index: int) -> str:
        cursor = index - 1
        while cursor >= 0 and self.tokens[cursor].text != ";":
            if self.tokens[cursor].upper in _DML_STARTERS:
                return self.tokens[cursor].upper
            cursor -= 1
        return ""

    def _upper(self, index: int) -> str:
        return self.tokens[index].upper if 0 <= index < len(self.tokens) else ""

    def _text(self, index: int) -> str:
        return self.tokens[index].text if 0 <= index < len(self.tokens) else ""

    def _kind(self, index: int) -> str:
        return self.tokens[index].kind if 0 <= index < len(self.tokens) else ""

class OraclePlsqlParser(OracleSqlParser):
    def package_name(self) -> str | None:
        for index, token in enumerate(self.tokens):
            if token.upper == "PACKAGE":
                cursor = index + 1
                if self._upper(cursor) == "BODY":
                    cursor += 1
                if self._kind(cursor) == "IDENT" and self._text(cursor + 1) == "." and self._kind(cursor + 2) == "IDENT":
                    cursor += 2
                if self._kind(cursor) == "IDENT":
                    return self.tokens[cursor].text.strip('"[]').upper()
        return None

    def routines(self) -> list[ParsedRoutineDeclaration]:
        routines: list[ParsedRoutineDeclaration] = []
        declaration_indexes: list[tuple[int, str, int]] = []
        for index, token in enumerate(self.tokens):
            if token.upper not in {"PROCEDURE", "FUNCTION"}:
                continue
            if index > 0 and self._upper(index - 1) == "END":
                continue
            parsed_name = self._parse_object(index + 1)
            if not parsed_name:
                continue
            raw_name, _, next_index = parsed_name
            declaration_indexes.append((index, raw_name.rsplit(".", 1)[-1].upper(), next_index))
        for position, (index, name, after_name_index) in enumerate(declaration_indexes):
            kind = self.tokens[index].upper
            parameter_block = self._parameter_block(after_name_index)
            end = self._routine_end(name, self.tokens[after_name_index - 1].end)
            if end <= self.tokens[after_name_index - 1].end and position + 1 < len(declaration_indexes):
                end = self.tokens[declaration_indexes[position + 1][0]].start
            elif end <= self.tokens[after_name_index - 1].end:
                end = len(self.text)
            routines.append(ParsedRoutineDeclaration(kind, name, parameter_block, self.tokens[index].start, end))
        return routines

    def triggers(self) -> list[ParsedTriggerDeclaration]:
        triggers: list[ParsedTriggerDeclaration] = []
        for index, token in enumerate(self.tokens):
            if token.upper != "TRIGGER":
                continue
            parsed_name = self._parse_object(index + 1)
            if not parsed_name:
                continue
            raw_name, _, cursor = parsed_name
            name = raw_name.rsplit(".", 1)[-1].upper()
            while cursor < len(self.tokens):
                if self._upper(cursor) == "ON":
                    parsed = self._parse_object(cursor + 1)
                    if parsed:
                        table, _, _ = parsed
                        triggers.append(ParsedTriggerDeclaration(name, table, token.start, self._plsql_end_after(token.start, name)))
                    break
                if self.tokens[cursor].text == ";":
                    break
                cursor += 1
        return triggers

    def synonyms(self) -> list[ParsedSynonymDeclaration]:
        synonyms: list[ParsedSynonymDeclaration] = []
        for index, token in enumerate(self.tokens):
            if token.upper != "SYNONYM":
                continue
            cursor = index + 1
            parsed_name = self._parse_object(cursor)
            if not parsed_name:
                continue
            name, start, cursor = parsed_name
            while cursor < len(self.tokens) and self._upper(cursor) != "FOR" and self.tokens[cursor].text != ";":
                cursor += 1
            if self._upper(cursor) != "FOR":
                continue
            parsed_target = self._parse_object(cursor + 1)
            if parsed_target:
                target, _, _ = parsed_target
                synonyms.append(ParsedSynonymDeclaration(name, target, start))
        return synonyms

    def _parameter_block(self, index: int) -> str | None:
        if self._text(index) != "(":
            return None
        start = self.tokens[index].start
        depth = 0
        cursor = index
        while cursor < len(self.tokens):
            if self._text(cursor) == "(":
                depth += 1
            elif self._text(cursor) == ")":
                depth -= 1
                if depth == 0:
                    return self.text[start:self.tokens[cursor].end]
            cursor += 1
        return None

    def _routine_end(self, name: str, after_offset: int) -> int:
        for match in re.finditer(r"\bEND\s+\"?([A-Za-z_][\w$#]*)\"?\s*;", self.text[after_offset:], re.IGNORECASE):
            if match.group(1).upper() == name:
                return after_offset + match.end()
        for match in re.finditer(r"\bEND(?:\s+\"?([A-Za-z_][\w$#]*)\"?)?\s*;", self.text[after_offset:], re.IGNORECASE):
            ended = (match.group(1) or "").upper()
            if ended not in {"IF", "LOOP", "CASE"}:
                return after_offset + match.end()
        return -1

    def _plsql_end_after(self, start_offset: int, name: str = "") -> int:
        body = self.text[start_offset:]
        if name:
            named = re.search(rf"\bEND\s+\"?{re.escape(name)}\"?\s*;", body, re.IGNORECASE)
            if named:
                return start_offset + named.end()
        for match in re.finditer(r"\bEND(?:\s+\"?([A-Za-z_][\w$#]*)\"?)?\s*;", body, re.IGNORECASE):
            ended = (match.group(1) or "").upper()
            if ended not in {"IF", "LOOP", "CASE"}:
                return start_offset + match.end()
        slash = re.search(r"^\s*/\s*$", body, re.MULTILINE)
        return start_offset + slash.start() if slash else len(self.text)

def _tokenize(text: str) -> Iterable[Token]:
    index = 0
    while index < len(text):
        char = text[index]
        pair = text[index:index + 2]
        if char.isspace():
            index += 1
            continue
        if pair == "--":
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end + 1
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            index = len(text) if end < 0 else end + 2
            continue
        if char in "nN" and index + 1 < len(text) and text[index + 1] == "'":
            index += 1
            while index < len(text):
                if text[index] == "'":
                    if index + 1 < len(text) and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char in "qQ" and index + 2 < len(text) and text[index + 1] == "'":
            opening = text[index + 2]
            closing = {"[": "]", "(": ")", "{": "}", "<": ">"}.get(opening, opening)
            end = text.find(closing + "'", index + 3)
            index = len(text) if end < 0 else end + 2
            continue
        if char == "'":
            index += 1
            while index < len(text):
                if text[index] == "'":
                    if index + 1 < len(text) and text[index + 1] == "'":
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char == '"':
            end = text.find('"', index + 1)
            end = len(text) - 1 if end < 0 else end
            raw = text[index:end + 1]
            yield Token(raw, raw.strip('"').upper(), index, end + 1, "IDENT")
            index = end + 1
            continue
        if char == "[":
            end = text.find("]", index + 1)
            end = len(text) - 1 if end < 0 else end
            raw = text[index:end + 1]
            yield Token(raw, raw.strip("[]").upper(), index, end + 1, "IDENT")
            index = end + 1
            continue
        ident = re.match(r"[A-Za-z_#$][\w$#]*", text[index:])
        if ident:
            raw = ident.group(0)
            yield Token(raw, raw.upper(), index, index + len(raw), "IDENT")
            index += len(raw)
            continue
        if char in ".,;()@":
            yield Token(char, char, index, index + 1, "PUNCT")
        index += 1

def _normalize_object(name: str) -> str:
    name = re.sub(r"\s*@\s*", "@", name.strip())
    parts = []
    for raw in re.split(r"\s*\.\s*", name):
        part = raw.strip().strip('"[]')
        if part:
            parts.append(part)
    return ".".join(parts)

def mask_noncode(text: str) -> str:
    """Replace comments and string literals with spaces while preserving offsets."""
    chars = list(text)
    index = 0
    state = "code"
    while index < len(chars):
        pair = text[index:index + 2]
        if state == "code":
            if text[index] in "nN" and index + 1 < len(text) and text[index + 1] == "'":
                chars[index:index + 2] = [" ", " "]
                index += 2
                state = "string"
                continue
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
                chars[index:index + 2] = [" ", " "]
                index += 2
                continue
            if pair == "/*":
                state = "block_comment"
                chars[index:index + 2] = [" ", " "]
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
                chars[index:index + 2] = [" ", " "]
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

def _is_system_object(name: str) -> bool:
    clean = name.split("@", 1)[0]
    parts = [part.strip('"[]').upper() for part in clean.split(".") if part]
    if not parts:
        return False
    leaf = parts[-1]
    owner = parts[-2] if len(parts) >= 2 else ""
    return (
        owner in _SYSTEM_OWNERS
        or leaf in _SYSTEM_OBJECTS
        or any(leaf.startswith(prefix) for prefix in _SYSTEM_OBJECT_PREFIXES)
    )
