"""ANTLR-ready PL/SQL call graph extractor.

This extractor is intentionally additive: it only emits CALLS edges that the
legacy regex extractor misses, especially same-package/local calls such as
``nRet := FunctionA(...)``.  The class is isolated so a future ANTLR visitor can
replace the lightweight fallback without changing the pipeline contract.
"""
from __future__ import annotations

import re
from pathlib import Path

from contract import schema as S
from contract.entities import ExtractionContext, ExtractionResult, GraphEdge
from extractors.base import BaseExtractor
from extractors.oracle_plsql.extractor import (
    _HAS_PLSQL,
    _PKG_RE,
    _PLSQL_EXTENSIONS,
    _SQL_EXTENSION,
    _line_of,
    _mask_string_literals,
    _resolve_active,
    _routine_declarations,
)

# Local/simple invocation candidate, e.g. FunctionA(...), LocalProc(...).
# Dotted calls are intentionally excluded here because OraclePlSqlExtractor
# already handles inter-package PKG.PROC(...) calls.
_LOCAL_CALL_RE = re.compile(
    r"(?<![\w$#\.])\"?([A-Za-z_][\w$#]*)\"?\s*\(",
    re.IGNORECASE,
)

# Tokens that are syntactically followed by parentheses but are not user calls.
_NON_CALL_NAMES: frozenset[str] = frozenset(
    {
        "ABS",
        "ADD_MONTHS",
        "AVG",
        "BEGIN",
        "CAST",
        "CEIL",
        "COALESCE",
        "COUNT",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "DECODE",
        "END",
        "EXISTS",
        "EXTRACT",
        "FLOOR",
        "GREATEST",
        "IF",
        "IN",
        "INSTR",
        "LAST_DAY",
        "LEAST",
        "LENGTH",
        "LOWER",
        "LPAD",
        "LTRIM",
        "MAX",
        "MIN",
        "MOD",
        "MONTHS_BETWEEN",
        "NVL",
        "NVL2",
        "RAISE_APPLICATION_ERROR",
        "REPLACE",
        "ROUND",
        "RPAD",
        "RTRIM",
        "SUBSTR",
        "SUM",
        "SYSDATE",
        "SYSTIMESTAMP",
        "TO_CHAR",
        "TO_DATE",
        "TO_NUMBER",
        "TRIM",
        "TRUNC",
        "UPPER",
        "VALUES",
        "WHEN",
        "WHILE",
    }
)


class OraclePlSqlAntlrCallExtractor(BaseExtractor):
    """Additive extractor for same-package/local PL/SQL CALLS edges.

    Until a generated PL/SQL parser is vendored, this uses narrow token-level
    matching. Extraction failures propagate so the scanner preserves old facts.
    """

    def can_handle(self, file_path: str, text: str) -> bool:
        suffix = Path(file_path).suffix.lower()
        return suffix in _PLSQL_EXTENSIONS or (suffix == _SQL_EXTENSION and bool(_HAS_PLSQL.search(text)))

    def extract(
        self,
        file_path: str,
        text: str,
        context: ExtractionContext,
    ) -> ExtractionResult:
        return self._extract_fallback(file_path, text, context)

    def _extract_fallback(
        self,
        file_path: str,
        text: str,
        context: ExtractionContext,
    ) -> ExtractionResult:
        result = ExtractionResult(source_file=file_path, extractor_name=type(self).__name__)

        pkg_match = _PKG_RE.search(text)
        pkg_name = pkg_match.group("package").upper() if pkg_match else ""
        pkg_schema = (
            (pkg_match.group("schema") or context.schema_name).upper()
            if pkg_match
            else context.schema_name.upper()
        )

        declarations = _routine_declarations(text, context, pkg_name, pkg_schema)
        spans = [(item.start, item.end, item.qname) for item in declarations]
        span_labels = {item.qname: item.label for item in declarations}
        local_symbols: dict[str, tuple[str, str]] = {}
        for item in declarations:
            local_symbols[item.name] = (item.label, item.qname)

        if not spans or not local_symbols:
            return result

        scan_text = _mask_string_literals(text)
        seen: set[tuple[str, str, int]] = set()

        for match in _LOCAL_CALL_RE.finditer(scan_text):
            name = match.group(1).upper()
            if name in _NON_CALL_NAMES or name not in local_symbols:
                continue
            if _is_dotted_call(scan_text, match.start()):
                continue
            line = _line_of(text, match.start())
            if _is_declaration_reference(scan_text, match.start()):
                continue
            caller_qname = _resolve_active(spans, match.start())
            if not caller_qname:
                continue
            target_label, target_qname = local_symbols[name]
            if caller_qname == target_qname:
                continue
            edge_key = (caller_qname, target_qname, line)
            if edge_key in seen:
                continue
            seen.add(edge_key)
            result.edges.append(
                GraphEdge(
                    from_label=span_labels.get(caller_qname, S.LABEL_PROCEDURE),
                    from_key="qualified_name",
                    from_key_value=caller_qname,
                    to_label=target_label,
                    to_key="qualified_name",
                    to_key_value=target_qname,
                    rel_type=S.REL_CALLS,
                    properties={
                        "call_type": "local_call",
                        "resolution": "same_package" if pkg_name else "same_file",
                        "confidence": "medium",
                        "line": line + 1,
                        "source_file": file_path,
                    },
                )
            )

        return result


def _is_dotted_call(text: str, start: int) -> bool:
    i = start - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return i >= 0 and text[i] == "."


def _is_declaration_reference(text: str, start: int) -> bool:
    prefix = text[max(0, start - 40):start]
    return bool(re.search(r"\b(?:FUNCTION|PROCEDURE)\s+$", prefix, re.IGNORECASE))
