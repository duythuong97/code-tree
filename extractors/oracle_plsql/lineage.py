"""Oracle PL/SQL column-lineage extractor.

Best-effort parser for common Oracle ETL patterns. No external dependency.
Focus:
  - INSERT INTO target(cols...) SELECT exprs... FROM ...
  - INSERT INTO target(cols...) VALUES (...)
  - UPDATE target alias SET col = expr ... FROM/WHERE subqueries
  - MERGE INTO target USING source ON ... UPDATE SET ... INSERT (...) VALUES (...)
  - cursor declarations / OPEN cursor FOR SELECT are represented as Cursor nodes

Produces Column nodes plus column-level evidence edges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from contract import schema as S
from contract.entities import ExtractionContext, ExtractionResult, GraphEdge, GraphNode
from contract.graph_contract import normalize_repository_path
from extractors.base import BaseExtractor
from extractors.oracle_plsql.extractor import _routine_declarations, _routine_end

_PLSQL_EXTENSIONS = {
    ".pks",
    ".pkb",
    ".pck",
    ".pls",
    ".plb",
    ".fnc",
    ".prc",
    ".trg",
    ".sql",
}
_HAS_SQL = re.compile(
    r"\b(INSERT|UPDATE|MERGE|CURSOR|OPEN|SELECT|FOR)\b", re.IGNORECASE
)
_PKG_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?PACKAGE\s+(?:BODY\s+)?"
    r'(?:"?(?P<schema>[\w$#]+)"?\s*\.\s*)?'
    r'"?(?P<package>[\w$#]+)"?\s*(?:AS|IS)\b',
    re.IGNORECASE,
)
_TRIGGER_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?TRIGGER\s+"
    r'(?:"?(?P<schema>[\w$#]+)"?\s*\.\s*)?'
    r'"?(?P<name>[\w$#]+)"?',
    re.IGNORECASE,
)
_CURSOR_RE = re.compile(
    r"\bCURSOR\s+(\w+)\s+IS\s+(SELECT\b.*?)(?=;)", re.IGNORECASE | re.DOTALL
)
_OPEN_FOR_RE = re.compile(
    r"\bOPEN\s+(\w+)\s+FOR\s+(SELECT\b.*?)(?=;)", re.IGNORECASE | re.DOTALL
)
_SELECT_INTO_RE = re.compile(
    r"\bSELECT\s+([^;]+?)\s+INTO\s+([^;]+?)\s+FROM\s+([^;]+?)(?=;)",
    re.IGNORECASE | re.DOTALL,
)
_FOR_SELECT_LOOP_RE = re.compile(
    r"\bFOR\s+(\w+)\s+IN\s*\(\s*(SELECT\b.+?)\)\s+LOOP\b", re.IGNORECASE | re.DOTALL
)

_ORACLE_WORDS = {
    "SELECT",
    "FROM",
    "WHERE",
    "JOIN",
    "LEFT",
    "RIGHT",
    "INNER",
    "OUTER",
    "FULL",
    "ON",
    "AND",
    "OR",
    "CASE",
    "WHEN",
    "THEN",
    "ELSE",
    "END",
    "NULL",
    "NVL",
    "COALESCE",
    "DECODE",
    "SUM",
    "MIN",
    "MAX",
    "COUNT",
    "AVG",
    "ROUND",
    "TRUNC",
    "TO_CHAR",
    "TO_DATE",
    "SYSDATE",
    "SYSTIMESTAMP",
    "ROWNUM",
    "ROWID",
    "IN",
    "EXISTS",
    "NOT",
    "IS",
    "AS",
    "DISTINCT",
    "GROUP",
    "ORDER",
    "BY",
    "HAVING",
    "UNION",
    "ALL",
}
_SKIP_TABLES = {"DUAL", "XMLTABLE", "TABLE", "SELECT", "WITH"}
_REL_WRITES_COLUMN = "WRITES_COLUMN"
_REL_READS_COLUMN = "READS_COLUMN"
_REL_DERIVES_FROM = "DERIVES_FROM"
_REL_POPULATES = "POPULATES"
_LABEL_CURSOR = "Cursor"


@dataclass
class _Span:
    start: int
    end: int
    qname: str
    label: str


@dataclass
class _SelectInfo:
    expressions: list[str]
    alias_to_table: dict[str, str]


@dataclass
class _VariableSource:
    table: str
    column: str
    expression: str
    line: int
    confidence: float


class OraclePlSqlLineageExtractor(BaseExtractor):
    def can_handle(self, file_path: str, text: str) -> bool:
        if Path(file_path).suffix.lower() not in _PLSQL_EXTENSIONS:
            return False
        return bool(_HAS_SQL.search(text[:100_000]))

    def extract(
        self, file_path: str, text: str, context: ExtractionContext
    ) -> ExtractionResult:
        result = ExtractionResult(
            source_file=file_path, extractor_name="OraclePlSqlLineageExtractor"
        )
        clean = _strip_comments_keep_strings(text)
        repository = context.repository
        ctx_schema = context.schema_name.upper()
        pkg_match = _PKG_RE.search(clean)
        pkg = pkg_match.group("package").upper() if pkg_match else ""
        pkg_schema = (
            (pkg_match.group("schema") or ctx_schema).upper()
            if pkg_match
            else ctx_schema
        )
        spans = [
            _Span(item.start, item.end, item.qname, item.label)
            for item in _routine_declarations(clean, context, pkg, pkg_schema)
        ]
        spans.extend(_trigger_spans(clean, context))
        if pkg_match:
            spans.append(
                _Span(
                    pkg_match.start(),
                    len(clean),
                    context.logic_qname(S.LABEL_PLSQL_PACKAGE, pkg, pkg_schema),
                    S.LABEL_PLSQL_PACKAGE,
                )
            )

        source_owner: _Span | None = None

        def owner_at(pos: int) -> _Span:
            nonlocal source_owner
            owner = _resolve_owner(spans, pos)
            if owner:
                return owner
            if source_owner is None:
                source_path = normalize_repository_path(context.relative_source_path or Path(file_path).name)
                source_owner = _Span(
                    0,
                    len(clean),
                    context.source_file_qname(),
                    S.LABEL_SOURCE_FILE,
                )
                _add_node(
                    result,
                    GraphNode(
                        S.LABEL_SOURCE_FILE,
                        "qualified_name",
                        source_owner.qname,
                        {
                            "qualified_name": source_owner.qname,
                            "name": PurePosixPath(source_path).name,
                            "repository": repository,
                            "source_id": context.source_id,
                            "source_path": source_path,
                            "layer": "logic",
                        },
                    ),
                )
            return source_owner

        variable_sources = _extract_variable_sources(
            result, clean, spans, owner_at, context, repository, ctx_schema, file_path
        )
        statements = _split_sql_statements(clean)
        for stmt, start_pos in statements:
            owner = owner_at(start_pos)
            up = stmt.lstrip().upper()
            if up.startswith("INSERT"):
                _extract_insert(
                    result,
                    stmt,
                    owner,
                    context,
                    repository,
                    ctx_schema,
                    file_path,
                    start_pos,
                    clean,
                    variable_sources,
                )
            elif up.startswith("UPDATE"):
                _extract_update(
                    result,
                    stmt,
                    owner,
                    context,
                    repository,
                    ctx_schema,
                    file_path,
                    start_pos,
                    clean,
                    variable_sources,
                )
            elif up.startswith("MERGE"):
                _extract_merge(
                    result,
                    stmt,
                    owner,
                    context,
                    repository,
                    ctx_schema,
                    file_path,
                    start_pos,
                    clean,
                    variable_sources,
                )

        _extract_cursors(
            result, clean, owner_at, context, repository, ctx_schema, file_path
        )
        return result


# ── Statement extractors ─────────────────────────────────────────────────────


def _extract_insert(
    result: ExtractionResult,
    stmt: str,
    owner: _Span,
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
    start_pos: int,
    full_text: str,
    variable_sources: dict[str, list[_VariableSource]],
) -> None:
    m = re.search(
        r"\bINSERT\s+(?:ALL\s+)?INTO\s+((?:\"?[\w$#]+\"?\s*\.\s*)?\"?[\w$#]+\"?)\s*(\([^)]*\))?",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return
    target_table = _norm_table(m.group(1), schema)
    target_cols = _parse_column_list(m.group(2) or "")
    after = stmt[m.end() :]
    select_pos = _find_keyword_top(after, "SELECT")
    values_pos = _find_keyword_top(after, "VALUES")
    line = _line_of(full_text, start_pos) + 1
    if select_pos >= 0:
        sel = _parse_select(after[select_pos:])
        for idx, target_col in enumerate(target_cols):
            expr = sel.expressions[idx] if idx < len(sel.expressions) else ""
            _add_lineage(
                result,
                owner,
                target_table,
                target_col,
                expr,
                sel.alias_to_table,
                context,
                repository,
                schema,
                file_path,
                line,
                "INSERT_SELECT",
                0.90,
                variable_sources,
            )
    elif values_pos >= 0:
        vals = _first_paren_content(after[values_pos:])
        exprs = _split_csv(vals)
        for idx, target_col in enumerate(target_cols):
            expr = exprs[idx] if idx < len(exprs) else ""
            _add_lineage(
                result,
                owner,
                target_table,
                target_col,
                expr,
                {},
                context,
                repository,
                schema,
                file_path,
                line,
                "INSERT_VALUES",
                0.65,
                variable_sources,
            )


def _extract_update(
    result: ExtractionResult,
    stmt: str,
    owner: _Span,
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
    start_pos: int,
    full_text: str,
    variable_sources: dict[str, list[_VariableSource]],
) -> None:
    m = re.search(
        r"\bUPDATE\s+((?:\"?[\w$#]+\"?\s*\.\s*)?\"?[\w$#]+\"?)(?:\s+(\w+))?\s+SET\s+",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return
    target_table = _norm_table(m.group(1), schema)
    target_alias = (m.group(2) or "").upper()
    set_start = m.end()
    where_pos = _find_keyword_top(stmt[set_start:], "WHERE")
    set_text = (
        stmt[set_start:] if where_pos < 0 else stmt[set_start : set_start + where_pos]
    )
    alias_map = _alias_map_from_from_join(stmt, schema)
    if target_alias:
        alias_map[target_alias] = target_table
    line = _line_of(full_text, start_pos) + 1
    for assign in _split_csv(set_text):
        am = re.match(
            r"\s*(?:(\w+)\.)?\"?([\w$#]+)\"?\s*=\s*(.+)\s*$",
            assign,
            re.IGNORECASE | re.DOTALL,
        )
        if not am:
            continue
        col = am.group(2).upper()
        expr = am.group(3).strip()
        _add_lineage(
            result,
            owner,
            target_table,
            col,
            expr,
            alias_map,
            context,
            repository,
            schema,
            file_path,
            line,
            "UPDATE_SET",
            0.82,
            variable_sources,
        )


def _extract_merge(
    result: ExtractionResult,
    stmt: str,
    owner: _Span,
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
    start_pos: int,
    full_text: str,
    variable_sources: dict[str, list[_VariableSource]],
) -> None:
    m = re.search(
        r"\bMERGE\s+INTO\s+((?:\"?[\w$#]+\"?\s*\.\s*)?\"?[\w$#]+\"?)(?:\s+(\w+))?\s+USING\s+",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return
    target_table = _norm_table(m.group(1), schema)
    target_alias = (m.group(2) or "").upper()
    alias_map = _alias_map_from_from_join(stmt, schema)
    if target_alias:
        alias_map[target_alias] = target_table
    using_alias = re.search(
        r"\bUSING\s+(?:\([^;]+?\)|((?:\w+\.)?\w+))\s+(\w+)\s+ON\b",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if using_alias and using_alias.group(1):
        alias_map[using_alias.group(2).upper()] = _norm_table(
            using_alias.group(1), schema
        )
    line = _line_of(full_text, start_pos) + 1

    upd = re.search(
        r"\bWHEN\s+MATCHED\s+THEN\s+UPDATE\s+SET\s+(.+?)(?=\bWHEN\s+NOT\s+MATCHED\b|$)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if upd:
        for assign in _split_csv(upd.group(1)):
            am = re.match(
                r"\s*(?:(\w+)\.)?\"?([\w$#]+)\"?\s*=\s*(.+)\s*$",
                assign,
                re.IGNORECASE | re.DOTALL,
            )
            if am:
                _add_lineage(
                    result,
                    owner,
                    target_table,
                    am.group(2).upper(),
                    am.group(3).strip(),
                    alias_map,
                    context,
                    repository,
                    schema,
                    file_path,
                    line,
                    "MERGE_UPDATE",
                    0.82,
                    variable_sources,
                )

    ins = re.search(
        r"\bWHEN\s+NOT\s+MATCHED\s+THEN\s+INSERT\s*(\([^)]*\))\s*VALUES\s*(\([^)]*\))",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if ins:
        cols = _parse_column_list(ins.group(1))
        vals = _split_csv(_strip_outer_parens(ins.group(2)))
        for idx, col in enumerate(cols):
            expr = vals[idx] if idx < len(vals) else ""
            _add_lineage(
                result,
                owner,
                target_table,
                col,
                expr,
                alias_map,
                context,
                repository,
                schema,
                file_path,
                line,
                "MERGE_INSERT",
                0.82,
                variable_sources,
            )


def _extract_cursors(
    result: ExtractionResult,
    text: str,
    owner_at,
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
) -> None:
    for regex, kind in (
        (_CURSOR_RE, "CURSOR_SELECT"),
        (_OPEN_FOR_RE, "OPEN_FOR_SELECT"),
    ):
        for m in regex.finditer(text):
            owner = owner_at(m.start())
            cursor_name = m.group(1).upper()
            select_sql = m.group(2)
            line = _line_of(text, m.start()) + 1
            cursor_qn = context.logic_qname(
                _LABEL_CURSOR,
                f"{owner.qname.rsplit(':', 1)[-1]}.{cursor_name}",
            )
            _add_node(
                result,
                GraphNode(
                    _LABEL_CURSOR,
                    "qualified_name",
                    cursor_qn,
                    {
                        "qualified_name": cursor_qn,
                        "name": cursor_name,
                        "repository": repository,
                        "source_file": file_path,
                        "line": line,
                        "kind": kind,
                        "layer": "logic",
                    },
                ),
            )
            result.edges.append(
                GraphEdge(
                    owner.label,
                    "qualified_name",
                    owner.qname,
                    _LABEL_CURSOR,
                    "qualified_name",
                    cursor_qn,
                    S.REL_CONTAINS,
                    {"line": line, "source_file": file_path, "operation": kind},
                )
            )
            sel = _parse_select(select_sql)
            for expr in sel.expressions:
                for src_table, src_col in _source_columns(
                    expr, sel.alias_to_table, schema
                ):
                    src_qn = _column_qname(context, src_table, src_col)
                    _add_column(result, context, repository, src_table, src_col)
                    result.edges.append(
                        GraphEdge(
                            _LABEL_CURSOR,
                            "qualified_name",
                            cursor_qn,
                            S.LABEL_COLUMN,
                            "qualified_name",
                            src_qn,
                            _REL_READS_COLUMN,
                            {
                                "line": line,
                                "source_file": file_path,
                                "expression": expr,
                                "confidence": 0.72,
                            },
                        )
                    )


def _extract_variable_sources(
    result: ExtractionResult,
    text: str,
    spans: list[_Span],
    owner_at,
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
) -> dict[str, list[_VariableSource]]:
    out: dict[str, list[_VariableSource]] = {}

    for m in _SELECT_INTO_RE.finditer(text):
        select_list = m.group(1)
        into_list = m.group(2)
        from_sql = "FROM " + m.group(3)
        line = _line_of(text, m.start()) + 1
        owner = owner_at(m.start())
        alias_map = _alias_map_from_from_join(from_sql, schema)
        exprs = _split_csv(select_list)
        vars_ = [_clean_variable(v) for v in _split_csv(into_list)]
        for idx, var in enumerate(vars_):
            expr = exprs[idx] if idx < len(exprs) else ""
            sources = _source_columns(expr, alias_map, schema)
            for table, col in sources:
                out.setdefault(var, []).append(
                    _VariableSource(table, col, expr, line, 0.76)
                )
                _add_column(result, context, repository, table, col)
                src_qn = _column_qname(context, table, col)
                result.edges.append(
                    GraphEdge(
                        owner.label,
                        "qualified_name",
                        owner.qname,
                        S.LABEL_COLUMN,
                        "qualified_name",
                        src_qn,
                        _REL_READS_COLUMN,
                        {
                            "operation": "SELECT_INTO",
                            "line": line,
                            "source_file": file_path,
                            "expression": expr,
                            "variable": var,
                            "confidence": 0.76,
                        },
                    )
                )

    for m in _FOR_SELECT_LOOP_RE.finditer(text):
        rec = m.group(1).upper()
        select_sql = m.group(2)
        line = _line_of(text, m.start()) + 1
        owner = owner_at(m.start())
        sel = _parse_select(select_sql, keep_alias=True)
        for expr in sel.expressions:
            field = _select_output_name(expr)
            if not field:
                continue
            for table, col in _source_columns(expr, sel.alias_to_table, schema):
                var = f"{rec}.{field}"
                out.setdefault(var, []).append(
                    _VariableSource(table, col, expr, line, 0.78)
                )
                _add_column(result, context, repository, table, col)
                src_qn = _column_qname(context, table, col)
                result.edges.append(
                    GraphEdge(
                        owner.label,
                        "qualified_name",
                        owner.qname,
                        S.LABEL_COLUMN,
                        "qualified_name",
                        src_qn,
                        _REL_READS_COLUMN,
                        {
                            "operation": "FOR_SELECT_LOOP",
                            "line": line,
                            "source_file": file_path,
                            "expression": expr,
                            "variable": var,
                            "confidence": 0.78,
                        },
                    )
                )
    return out


# ── Graph helpers ────────────────────────────────────────────────────────────


def _add_lineage(
    result: ExtractionResult,
    owner: _Span,
    target_table: str,
    target_col: str,
    expr: str,
    alias_map: dict[str, str],
    context: ExtractionContext,
    repository: str,
    schema: str,
    file_path: str,
    line: int,
    operation: str,
    confidence: float,
    variable_sources: dict[str, list[_VariableSource]] | None = None,
) -> None:
    if not target_col:
        return
    target_qn = _column_qname(context, target_table, target_col)
    _add_column(result, context, repository, target_table, target_col)
    result.edges.append(
        GraphEdge(
            owner.label,
            "qualified_name",
            owner.qname,
            S.LABEL_COLUMN,
            "qualified_name",
            target_qn,
            _REL_WRITES_COLUMN,
            {
                "operation": operation,
                "line": line,
                "source_file": file_path,
                "expression": expr,
                "confidence": confidence,
            },
        )
    )
    srcs = _source_columns(expr, alias_map, schema)
    var_srcs = _sources_from_variables(expr, variable_sources or {})
    if variable_sources:
        srcs = [
            (t, c)
            for t, c in srcs
            if not _is_record_variable_source(t, c, schema, variable_sources)
        ]
    if not srcs:
        if not var_srcs:
            result.edges.append(
                GraphEdge(
                    owner.label,
                    "qualified_name",
                    owner.qname,
                    S.LABEL_COLUMN,
                    "qualified_name",
                    target_qn,
                    _REL_POPULATES,
                    {
                        "operation": operation,
                        "line": line,
                        "source_file": file_path,
                        "expression": expr,
                        "confidence": min(confidence, 0.55),
                        "source_kind": "expression_or_variable",
                    },
                )
            )
            return
    for src_table, src_col in srcs + [(s.table, s.column) for s in var_srcs]:
        src_qn = _column_qname(context, src_table, src_col)
        _add_column(result, context, repository, src_table, src_col)
        result.edges.append(
            GraphEdge(
                S.LABEL_COLUMN,
                "qualified_name",
                src_qn,
                S.LABEL_COLUMN,
                "qualified_name",
                target_qn,
                _REL_DERIVES_FROM,
                {
                    "operation": operation,
                    "line": line,
                    "source_file": file_path,
                    "expression": expr,
                    "confidence": confidence,
                },
            )
        )
        result.edges.append(
            GraphEdge(
                owner.label,
                "qualified_name",
                owner.qname,
                S.LABEL_COLUMN,
                "qualified_name",
                src_qn,
                _REL_READS_COLUMN,
                {
                    "operation": operation,
                    "line": line,
                    "source_file": file_path,
                    "expression": expr,
                    "confidence": confidence,
                },
            )
        )


def _add_column(
    result: ExtractionResult,
    context: ExtractionContext,
    repository: str,
    table_full: str,
    col: str,
) -> None:
    table_name = table_full.split(".")[-1]
    schema = table_full.rsplit(".", 1)[0] if "." in table_full else ""
    table_qn = context.table_qname(table_full)
    col_qn = _column_qname(context, table_full, col)
    _add_node(
        result,
        GraphNode(
            S.LABEL_TABLE,
            "qualified_name",
            table_qn,
            {
                "qualified_name": table_qn,
                "name": table_name,
                "schema": schema or None,
                "repository": repository,
                "db_name": context.db_name,
                "layer": "data",
            },
        ),
    )
    _add_node(
        result,
        GraphNode(
            S.LABEL_COLUMN,
            "qualified_name",
            col_qn,
            {
                "qualified_name": col_qn,
                "name": col,
                "table_name": table_name,
                "table_qname": table_qn,
                "schema": schema or None,
                "repository": repository,
                "db_name": context.db_name,
                "layer": "data",
            },
        ),
    )
    edge_key = (table_qn, col_qn, S.REL_CONTAINS)
    if edge_key not in {
        (e.from_key_value, e.to_key_value, e.rel_type) for e in result.edges
    }:
        result.edges.append(
            GraphEdge(
                S.LABEL_TABLE,
                "qualified_name",
                table_qn,
                S.LABEL_COLUMN,
                "qualified_name",
                col_qn,
                S.REL_CONTAINS,
                {},
            )
        )


def _column_qname(context: ExtractionContext, table_full: str, col: str) -> str:
    return (
        f"Column:{context.table_qname(table_full).removeprefix('Table:')}:{col.upper()}"
    )


def _logic_node(
    label: str, qname: str, name: str, repository: str, file_path: str
) -> GraphNode:
    return GraphNode(
        label,
        "qualified_name",
        qname,
        {
            "qualified_name": qname,
            "name": name,
            "repository": repository,
            "source_file": file_path,
            "layer": "logic",
        },
    )


# ── SQL parsing helpers ──────────────────────────────────────────────────────


def _parse_select(sql: str, keep_alias: bool = False) -> _SelectInfo:
    body = sql.strip().rstrip(";")
    if body.startswith("(") and body.endswith(")"):
        body = _strip_outer_parens(body)
    select_idx = _find_keyword_top(body, "SELECT")
    from_idx = _find_keyword_top(body, "FROM")
    if select_idx < 0 or from_idx < 0 or from_idx <= select_idx:
        return _SelectInfo([], {})
    select_list = body[select_idx + 6 : from_idx]
    rest = body[from_idx:]
    expressions = (
        _split_csv_keep_alias(select_list) if keep_alias else _split_csv(select_list)
    )
    return _SelectInfo(expressions, _alias_map_from_from_join(rest, ""))


def _alias_map_from_from_join(sql: str, default_schema: str) -> dict[str, str]:
    out: dict[str, str] = {}
    pat = re.compile(
        r"\b(?:FROM|JOIN|USING)\s+((?:\"?[\w$#]+\"?\s*\.\s*)?\"?[\w$#]+\"?(?:@[\w$#]+)?)(?:\s+(?:AS\s+)?(\w+))?",
        re.IGNORECASE,
    )
    for m in pat.finditer(sql):
        table = _norm_table(m.group(1), default_schema)
        if table.split(".")[-1].upper() in _SKIP_TABLES:
            continue
        alias = (m.group(2) or table.split(".")[-1]).upper()
        if alias not in {
            "WHERE",
            "ON",
            "JOIN",
            "LEFT",
            "RIGHT",
            "INNER",
            "FULL",
            "GROUP",
            "ORDER",
        }:
            out[alias] = table
    return out


def _source_columns(
    expr: str, alias_map: dict[str, str], default_schema: str
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in re.finditer(r"\b(\w+)\s*\.\s*\"?([\w$#]+)\"?", expr):
        prefix = m.group(1).upper()
        col = m.group(2).upper()
        if prefix in _ORACLE_WORDS or col in _ORACLE_WORDS:
            continue
        table = alias_map.get(prefix)
        if not table:
            table = _norm_table(prefix, default_schema)
        item = (table, col)
        if item not in seen:
            seen.add(item)
            found.append(item)
    return found


def _sources_from_variables(
    expr: str, variable_sources: dict[str, list[_VariableSource]]
) -> list[_VariableSource]:
    out: list[_VariableSource] = []
    seen: set[tuple[str, str, str]] = set()
    tokens = {
        _clean_variable(m.group(0))
        for m in re.finditer(r"\b\w+(?:\s*\.\s*\w+)?\b", expr)
    }
    for token in tokens:
        for src in variable_sources.get(token, []):
            key = (src.table, src.column, src.expression)
            if key not in seen:
                seen.add(key)
                out.append(src)
    return out


def _is_record_variable_source(
    table: str,
    col: str,
    schema: str,
    variable_sources: dict[str, list[_VariableSource]],
) -> bool:
    table_name = table.split(".")[-1].upper()
    return f"{table_name}.{col.upper()}" in variable_sources and table == _norm_table(
        table_name, schema
    )


def _clean_variable(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().strip('"')).upper()


def _select_output_name(expr: str) -> str:
    e = expr.strip()
    m = re.search(r"\s+AS\s+\"?([\w$#]+)\"?\s*$", e, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    m = re.search(r"\s+\"?([A-Za-z_][\w$#]*)\"?\s*$", e)
    if m and not e.upper().endswith(")"):
        return m.group(1).upper()
    m = re.search(r"\.\s*\"?([\w$#]+)\"?\s*$", e)
    if m:
        return m.group(1).upper()
    return ""


def _split_sql_statements(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    start = 0
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif ch == ";" and depth == 0:
                stmt = text[start:i].strip()
                m = re.search(r"\b(INSERT|UPDATE|MERGE)\b", stmt, re.IGNORECASE)
                if m:
                    out.append((stmt[m.start() :], start + m.start()))
                start = i + 1
        i += 1
    return out


def _split_csv(text: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif not in_str and ch == "(":
            depth += 1
            buf.append(ch)
        elif not in_str and ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif not in_str and ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                items.append(_strip_alias(part))
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        items.append(_strip_alias(tail))
    return items


def _split_csv_keep_alias(text: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_str = not in_str
            buf.append(ch)
        elif not in_str and ch == "(":
            depth += 1
            buf.append(ch)
        elif not in_str and ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif not in_str and ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                items.append(part)
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _strip_alias(expr: str) -> str:
    if re.fullmatch(r"\s*\w+\s*\.\s*\"?[\w$#]+\"?\s*", expr):
        return expr.strip()
    return re.sub(
        r"\s+(?:AS\s+)?\"?[A-Za-z_][\w$#]*\"?\s*$",
        "",
        expr.strip(),
        flags=re.IGNORECASE,
    )


def _parse_column_list(text: str) -> list[str]:
    body = _strip_outer_parens(text.strip()) if text.strip().startswith("(") else text
    return [
        c.strip().strip('"').split(".")[-1].upper()
        for c in _split_csv(body)
        if c.strip()
    ]


def _first_paren_content(text: str) -> str:
    idx = text.find("(")
    if idx < 0:
        return ""
    depth = 0
    for i in range(idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[idx + 1 : i]
    return ""


def _strip_outer_parens(text: str) -> str:
    t = text.strip()
    if t.startswith("(") and t.endswith(")"):
        return t[1:-1].strip()
    return t


def _find_keyword_top(text: str, keyword: str) -> int:
    kw = keyword.upper()
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            in_str = not in_str
        elif not in_str:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif depth == 0 and text[i : i + len(kw)].upper() == kw:
                before = text[i - 1] if i else " "
                after = text[i + len(kw)] if i + len(kw) < len(text) else " "
                if not (before.isalnum() or before == "_") and not (
                    after.isalnum() or after == "_"
                ):
                    return i
        i += 1
    return -1


# ── Generic helpers ──────────────────────────────────────────────────────────



def _resolve_owner(spans: list[_Span], pos: int) -> _Span | None:
    active = [span for span in spans if span.start <= pos <= span.end]
    return max(active, key=lambda span: span.start) if active else None

def _trigger_spans(text: str, context: ExtractionContext) -> list[_Span]:
    return [
        _Span(
            m.start(),
            _routine_end(text, m.start()),
            context.logic_qname(
                S.LABEL_TRIGGER,
                m.group("name"),
                m.group("schema") or context.schema_name,
            ),
            S.LABEL_TRIGGER,
        )
        for m in _TRIGGER_RE.finditer(text)
    ]


def _strip_comments_keep_strings(text: str) -> str:
    text = re.sub(
        r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.DOTALL
    )
    return re.sub(r"--[^\n]*", "", text)


def _norm_table(name: str, default_schema: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*\.\s*", name.strip())]
    normalized = [
        part if part.startswith('"') and part.endswith('"') else part.upper()
        for part in parts
        if part
    ]
    raw = ".".join(normalized)
    if len(normalized) > 1:
        return raw
    return f"{default_schema}.{raw}" if default_schema else raw


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n")


def _first_group(regex: re.Pattern, text: str) -> str:
    m = regex.search(text)
    return m.group(1) if m else ""


def _add_node(result: ExtractionResult, node: GraphNode) -> None:
    if node.key_value not in {n.key_value for n in result.nodes}:
        result.nodes.append(node)
