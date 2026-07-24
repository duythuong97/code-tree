from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Comment:
    comment_id: str
    source_path: str
    language: str
    comment_kind: str
    classification: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    raw_text: str
    normalized_text: str


_C_STYLE = re.compile(r"//[^\r\n]*|/\*[\s\S]*?\*/")
_HTML = re.compile(r"<!--[\s\S]*?-->")
_SQL = re.compile(r"--[^\r\n]*|/\*[\s\S]*?\*/")
_DIRECTIVE = re.compile(r"(?:TODO|FIXME|HACK|NOTE|WARNING|noinspection|eslint|tslint|pragma|region|#if|#endif)", re.IGNORECASE)
_DISABLED_CODE = re.compile(r"(?:\b(?:if|for|while|return|class|public|private|SELECT|INSERT|UPDATE|DELETE|BEGIN|END)\b|[;{}]|=>)", re.IGNORECASE)


def extract_comments(text: str, source_path: str, source_type: str) -> list[Comment]:
    language = _language(source_type, source_path)
    pattern = _HTML if language == "xml" else _SQL if language in {"sql", "plsql"} else _C_STYLE
    comments: list[Comment] = []
    for match in pattern.finditer(text):
        raw = match.group(0)
        start_line, start_column = _position(text, match.start())
        end_line, end_column = _position(text, max(match.start(), match.end() - 1))
        kind = _kind(raw, language)
        normalized = _normalize(raw, kind)
        classification = _classify(raw, normalized, kind)
        digest = hashlib.sha256(f"{source_path}|{match.start()}|{match.end()}|{raw}".encode("utf-8")).hexdigest()
        comments.append(Comment(
            comment_id=f"comment:{digest}", source_path=source_path, language=language,
            comment_kind=kind, classification=classification,
            start_line=start_line, end_line=end_line,
            start_column=start_column, end_column=end_column,
            raw_text=raw, normalized_text=normalized,
        ))
    return comments


def _language(source_type: str, source_path: str) -> str:
    suffix = source_path.lower().rsplit(".", 1)[-1] if "." in source_path else ""
    if suffix in {"xml", "html"} or source_type == "xml-sql":
        return "xml"
    if source_type in {"plsql", "oracle-plsql"} or suffix in {"pks", "pkb", "pls", "plb"}:
        return "plsql"
    if source_type in {"sql", "sql-file", "sql-loader"} or suffix in {"sql", "ctl"}:
        return "sql"
    if suffix == "cs":
        return "csharp"
    return "typescript"


def _kind(raw: str, language: str) -> str:
    if raw.startswith("<!--"):
        return "BLOCK"
    if raw.startswith("/**") or raw.startswith("///"):
        return "DOCUMENTATION"
    if raw.startswith("/*"):
        return "BLOCK"
    return "LINE"


def _normalize(raw: str, kind: str) -> str:
    value = raw
    if value.startswith("<!--"):
        value = value[4:-3]
    elif value.startswith("/*"):
        value = value[2:-2]
    elif value.startswith("//") or value.startswith("--"):
        value = value[2:]
    lines = [re.sub(r"^\s*\*?\s?", "", line).rstrip() for line in value.splitlines()]
    return "\n".join(lines).strip()


def _classify(raw: str, normalized: str, kind: str) -> str:
    if kind == "DOCUMENTATION" or re.search(r"@(param|returns?|summary|remarks?)\b", raw, re.IGNORECASE):
        return "DOCUMENTATION"
    if _DIRECTIVE.search(raw):
        return "DIRECTIVE"
    if _DISABLED_CODE.search(normalized):
        return "DISABLED_CODE"
    if len(normalized) >= 40 or re.search(r"[ぁ-んァ-ヶ一-龠]", normalized):
        return "BUSINESS"
    return "ORDINARY"


def _position(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    previous = text.rfind("\n", 0, offset)
    return line, offset - previous
