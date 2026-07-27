"""Oracle PL/SQL extractor.

Parses Oracle PL/SQL source files: packages, standalone procedures/functions,
triggers. Extracts SQL DML statements and links them to the enclosing
procedure or function via table-level edges only. Column detail belongs in
drill-down tables, not the main flow graph.

File extensions handled:
  .pks  — package specification
  .pkb  — package body
  .pck  — package (combined spec + body)
  .pls  — PL/SQL source
  .plb  — PL/SQL library (wrapped/unwrapped)
  .fnc  — standalone function
  .prc  — standalone procedure
  .trg  — trigger
  .sql  — if content contains CREATE OR REPLACE PACKAGE / PROCEDURE / FUNCTION / TRIGGER

Creates:
  - Class node  per package  (LABEL_CLASS,   object_type="PACKAGE")
  - Function node per proc/func inside package  (LABEL_FUNCTION)
  - Function node for trigger body              (LABEL_FUNCTION, proc_type="TRIGGER")
  - Table nodes referenced by SQL              (LABEL_TABLE)
    - Edges: BELONGS_TO, READS_FROM, INSERTS_INTO, UPDATES, DELETES_FROM, MERGES_INTO
    - Sequence nodes used by NEXTVAL/CURRVAL plus USES_SEQUENCE edges
  - Edge:  TRIGGERS (TriggerFn→Table  — the table that fires the trigger)
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from pathlib import Path

from code_tree_exporter.contract import schema as S
from code_tree_exporter.contract.entities import ExtractionContext, ExtractionResult, GraphEdge, GraphNode
from code_tree_exporter.extractors.base import BaseExtractor

logger = logging.getLogger(__name__)

# ── File type detection ───────────────────────────────────────────────────────
_PLSQL_EXTENSIONS = {".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg"}
_SQL_EXTENSION = ".sql"

_HAS_PLSQL = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?"
    r"(?:PACKAGE|PROCEDURE|FUNCTION|TRIGGER)\b",
    re.IGNORECASE,
)

# ── Structural patterns ───────────────────────────────────────────────────────

# CREATE [OR REPLACE] PACKAGE [BODY] [schema.]name  AS|IS
_PKG_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?PACKAGE\s+(?:BODY\s+)?"
    r'(?:"?(?P<schema>[\w$#]+)"?\s*\.\s*)?'
    r'"?(?P<package>[\w$#]+)"?\s*(?:AS|IS)\b',
    re.IGNORECASE,
)

# Identifier fragment used by SQL object patterns. Supports quoted identifiers
# with spaces, e.g. "Emp Log".
_IDENT = r'(?:"[^"]+"|[\w$#]+)'

# PROCEDURE name  (may be in spec or body — we want both for the span map)
_PROC_RE = re.compile(
    r'\bPROCEDURE\s+(?:(?:"?(?P<schema>[\w$#]+)"?)\s*\.\s*)?'
    r'"?(?P<name>[\w$#]+)"?\s*(?P<params>\([^;]*?\))?',
    re.IGNORECASE,
)

# FUNCTION name  RETURN ...
_FUNC_RE = re.compile(
    r'\bFUNCTION\s+(?:(?:"?(?P<schema>[\w$#]+)"?)\s*\.\s*)?'
    r'"?(?P<name>[\w$#]+)"?\s*(?:\([^)]{0,300}\))?\s*RETURN\b',
    re.IGNORECASE | re.DOTALL,
)

# CREATE [OR REPLACE] TRIGGER name  BEFORE|AFTER|INSTEAD  dml_event  ON [schema.]table
_TRIGGER_RE = re.compile(
    r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?TRIGGER\s+"
    r'(?:"?(?P<trigger_schema>[\w$#]+)"?\s*\.\s*)?'
    r'"?(?P<trigger>[\w$#]+)"?\s*\n?'
    r"\s*(?:BEFORE|AFTER|INSTEAD\s+OF)\s+"
    r"(?:INSERT|UPDATE|DELETE|INSERT\s+OR\s+UPDATE|INSERT\s+OR\s+DELETE"
    r"|UPDATE\s+OR\s+DELETE|INSERT\s+OR\s+UPDATE\s+OR\s+DELETE)"
    r"(?:\s+OF\s+[\w$#,\s]+)?\s+ON\s+"
    r'(?:"?(?P<table_schema>[\w$#]+)"?\s*\.\s*)?'
    r'"?(?P<table>[\w$#]+)"?',
    re.IGNORECASE | re.DOTALL,
)

# ── SQL DML patterns (Oracle-specific) ────────────────────────────────────────

# INSERT [ALL] INTO [schema.]table
_SQL_INSERT = re.compile(
    r"\bINSERT\s+(?:ALL\s+)?INTO\s+"
    rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# UPDATE [schema.]table  SET
_SQL_UPDATE = re.compile(
    r"\bUPDATE\s+(?:"
    rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)'
    r')(?:\s+"?[\w$#]+"?)?\s+SET\b',
    re.IGNORECASE,
)

# DELETE FROM [schema.]table
_SQL_DELETE = re.compile(
    r"\bDELETE\s+FROM\s+" rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# MERGE INTO [schema.]table
_SQL_MERGE = re.compile(
    r"\bMERGE\s+INTO\s+" rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# FROM [schema.]table  — excludes table-function calls e.g. TABLE(...)
_SQL_FROM = re.compile(
    r"\bFROM\s+" rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)' r"(?!\s*\()",
    re.IGNORECASE,
)

# [LEFT|RIGHT|INNER|OUTER|CROSS|FULL] JOIN [schema.]table
_SQL_JOIN = re.compile(
    r"\bJOIN\s+" rf'(({_IDENT}\s*\.\s*)?{_IDENT}(?:@[\w$#]+)?)',
    re.IGNORECASE,
)

# EXECUTE IMMEDIATE 'literal sql'  (not EXECUTE IMMEDIATE variable)
_EXEC_IMMEDIATE = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\s+'([^']+)'",
    re.IGNORECASE,
)

_SEQ_USAGE = re.compile(
    r'((?:"?[\w$#]+"?\s*\.\s*)?"?[\w$#]+"?)\s*\.\s*(?:NEXTVAL|CURRVAL)\b',
    re.IGNORECASE,
)

_CONSTANT_RE = re.compile(
    r"^\s*\"?([A-Za-z_][\w$#]*)\"?\s+CONSTANT\s+(.+?)\s*(?::=\s*(.+?))?\s*;",
    re.IGNORECASE | re.MULTILINE,
)
_VARIABLE_RE = re.compile(
    r"^\s*\"?([A-Za-z_][\w$#]*)\"?\s+((?!(?:CONSTANT|TYPE|PROCEDURE|FUNCTION|CURSOR)\b).+?)\s*(?::=\s*(.+?))?\s*;",
    re.IGNORECASE | re.MULTILINE,
)
_TYPE_RE = re.compile(
    r"^\s*TYPE\s+\"?([A-Za-z_][\w$#]*)\"?\s+IS\s+(.+?);",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_DYNAMIC_SQL_ASSIGN_RE = re.compile(
    r"\b([A-Za-z_][\w$#]*)\s*:=\s*((?:'[^']*'\s*(?:\|\|\s*)?)+)\s*;",
    re.IGNORECASE,
)
_EXEC_IMMEDIATE_VAR = re.compile(
    r"\bEXECUTE\s+IMMEDIATE\s+([A-Za-z_][\w$#]*)\b",
    re.IGNORECASE,
)

# Inter-package call at statement level: PKG_NAME.PROC_NAME(
# Anchored to statement start (after ; or BEGIN/THEN/ELSE/LOOP/newline+spaces)
_PKG_CALL_RE = re.compile(
    r"(?:^|;|\bBEGIN\b|\bTHEN\b|\bELSE\b|\bLOOP\b|\bRETURN\b)\s+"
    r"(?:(?P<schema>[A-Z][A-Z0-9_$#]{1,29})\.)?"
    r"(?P<package>[A-Z][A-Z0-9_$#]{2,29})\."
    r"(?P<routine>[A-Z][A-Z0-9_$#]{1,29})\s*\(",
    re.IGNORECASE | re.MULTILINE,
)

# Oracle built-in packages to exclude from inter-package call detection
_ORACLE_BUILTIN_PKGS: frozenset[str] = frozenset(
    {
        "DBMS_OUTPUT",
        "DBMS_SQL",
        "DBMS_LOB",
        "DBMS_UTILITY",
        "DBMS_METADATA",
        "DBMS_LOCK",
        "DBMS_ALERT",
        "DBMS_PIPE",
        "DBMS_SCHEDULER",
        "DBMS_JOB",
        "DBMS_CRYPTO",
        "DBMS_RANDOM",
        "DBMS_TRANSACTION",
        "DBMS_XMLGEN",
        "UTL_FILE",
        "UTL_HTTP",
        "UTL_SMTP",
        "UTL_RAW",
        "UTL_I18N",
        "UTL_URL",
        "APEX_APPLICATION",
        "APEX_UTIL",
        "APEX_JSON",
        "APEX_ITEM",
        "SYS",
        "STANDARD",
    }
)

# PL/SQL keywords that can precede a dot and look like package names
_PLSQL_KW_PREFIXES: frozenset[str] = frozenset(
    {
        "IF",
        "END",
        "ELSIF",
        "EXCEPTION",
        "WHEN",
        "INTO",
        "FROM",
        "HAVING",
        "GROUP",
        "ORDER",
        "WHERE",
        "ON",
        "SET",
        "IN",
        "OUT",
    }
)

# Oracle objects to skip (pseudo-tables, system catalog, built-in functions)
_SKIP_NAMES: frozenset[str] = frozenset(
    {
        "DUAL",
        "ROWNUM",
        "ROWID",
        "SYSDATE",
        "SYSTIMESTAMP",
        "LEVEL",
        "XMLTABLE",
        "TABLE",
        "VIEW",
        "INDEX",
        "SELECT",
        "WITH",
    }
)
_SYS_PREFIX_RE = re.compile(
    r"^(?:SYS_|ALL_|DBA_|USER_|V\$|GV\$|DBMS_|UTL_|APEX_|MVIEW)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _RoutineDeclaration:
    start: int
    end: int
    qname: str
    label: str
    name: str
    schema: str
    match: re.Match[str]


class OraclePlSqlExtractor(BaseExtractor):
    def can_handle(self, file_path: str, text: str) -> bool:
        ext = Path(file_path).suffix.lower()
        if ext in _PLSQL_EXTENSIONS:
            return True
        if ext == _SQL_EXTENSION:
            return bool(_HAS_PLSQL.search(text[:3_000]))
        return False

    def extract(
        self, file_path: str, text: str, context: ExtractionContext
    ) -> ExtractionResult:
        result = ExtractionResult(
            source_file=file_path, extractor_name="OraclePlSqlExtractor"
        )
        repository = context.repository
        service = context.service_name or context.infer_service_from_path(file_path)
        ctx_schema = context.schema_name.upper()

        # ── Package node ──────────────────────────────────────────────────────
        pkg_name: str | None = None
        pkg_qname: str | None = None
        pkg_schema = ctx_schema
        pm = _PKG_RE.search(text)
        if pm:
            pkg_name = pm.group("package").upper()
            pkg_schema = (pm.group("schema") or ctx_schema).upper()
            pkg_qname = context.logic_qname(
                S.LABEL_PLSQL_PACKAGE, pkg_name, pkg_schema
            )
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PLSQL_PACKAGE,
                    key="qualified_name",
                    key_value=pkg_qname,
                    properties={
                        "qualified_name": pkg_qname,
                        "name": pkg_name,
                        "schema": pkg_schema or None,
                        "db_name": context.db_name,
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "object_type": "PACKAGE",
                        "layer": "logic",
                    },
                ),
            )

        # Bounded lexical spans prevent one routine from owning later routines.
        span_ranges: list[tuple[int, int, str]] = []
        span_labels: dict[str, str] = {}

        for declaration in _routine_declarations(
            text, context, pkg_name or "", pkg_schema
        ):
            m = declaration.match
            parent_qname = _resolve_active(span_ranges, declaration.start)
            span_ranges.append(
                (declaration.start, declaration.end, declaration.qname)
            )
            span_labels[declaration.qname] = declaration.label
            properties = {
                "qualified_name": declaration.qname,
                "name": declaration.name,
                "schema": declaration.schema or None,
                "db_name": context.db_name,
                "package": pkg_name or "",
                "service": service,
                "repository": repository,
                "source_file": file_path,
                "line": _line_of(text, declaration.start) + 1,
                "proc_type": (
                    "PROCEDURE"
                    if declaration.label == S.LABEL_PROCEDURE
                    else "FUNCTION"
                ),
                "layer": "logic",
            }
            if declaration.label == S.LABEL_PROCEDURE:
                properties["parameters"] = _parse_parameters(m.group("params") or "")
            _add_unique(
                result,
                GraphNode(
                    declaration.label,
                    "qualified_name",
                    declaration.qname,
                    properties,
                ),
            )
            if pkg_qname and not parent_qname:
                result.edges.append(
                    GraphEdge(
                        declaration.label,
                        "qualified_name",
                        declaration.qname,
                        S.LABEL_PLSQL_PACKAGE,
                        "qualified_name",
                        pkg_qname,
                        S.REL_BELONGS_TO,
                    )
                )

        source_owner_qname: str | None = None

        def source_owner() -> tuple[str, str]:
            nonlocal source_owner_qname
            if source_owner_qname is None:
                source_owner_qname = context.source_file_qname()
                source_path = normalize_repository_path(context.relative_source_path or Path(file_path).name)
                _add_unique(
                    result,
                    GraphNode(
                        S.LABEL_SOURCE_FILE,
                        "qualified_name",
                        source_owner_qname,
                        {
                            "qualified_name": source_owner_qname,
                            "name": PurePosixPath(source_path).name,
                            "repository": repository,
                            "source_id": context.source_id,
                            "source_path": source_path,
                            "layer": "logic",
                        },
                    ),
                )
            return source_owner_qname, S.LABEL_SOURCE_FILE

        def owner_at(pos: int, *, allow_package: bool = True) -> tuple[str, str]:
            qname = _resolve_active(span_ranges, pos)
            if qname:
                return qname, span_labels[qname]
            if allow_package and pkg_qname:
                return pkg_qname, S.LABEL_PLSQL_PACKAGE
            return source_owner()

        # Trigger bodies are bounded owners, not top-level SourceFile evidence.
        for m in _TRIGGER_RE.finditer(text):
            trg_name = m.group("trigger").upper()
            trigger_schema = (m.group("trigger_schema") or ctx_schema).upper()
            fired_on = ".".join(
                part
                for part in (
                    (m.group("table_schema") or "").upper(),
                    m.group("table").upper(),
                )
                if part
            )
            if _skip(fired_on):
                continue
            trg_qname = context.logic_qname(
                S.LABEL_TRIGGER, trg_name, trigger_schema
            )
            span_ranges.append((m.start(), _routine_end(text, m.start()), trg_qname))
            span_labels[trg_qname] = S.LABEL_TRIGGER
            _add_unique(
                result,
                GraphNode(
                    S.LABEL_TRIGGER,
                    "qualified_name",
                    trg_qname,
                    {
                        "qualified_name": trg_qname,
                        "name": trg_name,
                        "schema": trigger_schema or None,
                        "db_name": context.db_name,
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "proc_type": "TRIGGER",
                        "trigger_on_table": fired_on,
                        "layer": "logic",
                    },
                ),
            )
            table_schema, table_name, unresolved = context.resolved_object(fired_on)
            table_qname = context.table_qname(fired_on)
            _add_unique(
                result,
                GraphNode(
                    S.LABEL_TABLE,
                    "qualified_name",
                    table_qname,
                    {
                        "qualified_name": table_qname,
                        "name": table_name,
                        "schema": table_schema,
                        "schema_unresolved": unresolved,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )
            result.edges.append(
                GraphEdge(
                    S.LABEL_TRIGGER,
                    "qualified_name",
                    trg_qname,
                    S.LABEL_TABLE,
                    "qualified_name",
                    table_qname,
                    S.REL_TRIGGERS,
                    {"framework": "Oracle Trigger", "source_file": file_path},
                )
            )

        scan_text = _mask_string_literals(text)

        # ── Constants ───────────────────────────────────────────────────────
        for m in _CONSTANT_RE.finditer(text):
            name = m.group(1).upper()
            line_no = _line_of(text, m.start())
            owner_qname, owner_label = owner_at(m.start())
            scope = (
                owner_qname.rsplit(":", 1)[-1]
                if owner_label != S.LABEL_SOURCE_FILE
                else normalize_repository_path(context.relative_source_path or Path(file_path).name).upper()
            )
            qname = context.logic_qname(
                S.LABEL_PLSQL_CONSTANT, f"{scope}.{name}", pkg_schema
            )
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_PLSQL_CONSTANT,
                    key="qualified_name",
                    key_value=qname,
                    properties={
                        "qualified_name": qname,
                        "name": name,
                        "schema": pkg_schema or None,
                        "db_name": context.db_name,
                        "data_type": re.sub(r"\s+", " ", (m.group(2) or "").strip()).upper(),
                        "value": (m.group(3) or "").strip(),
                        "package": pkg_name or "",
                        "service": service,
                        "repository": repository,
                        "source_file": file_path,
                        "line": line_no + 1,
                        "layer": "logic",
                    },
                ),
            )
            result.edges.append(
                GraphEdge(
                    S.LABEL_PLSQL_CONSTANT,
                    "qualified_name",
                    qname,
                    owner_label,
                    "qualified_name",
                    owner_qname,
                    S.REL_BELONGS_TO,
                    {"line": line_no + 1, "source_file": file_path},
                )
            )

        for regex, label in (
            (_VARIABLE_RE, S.LABEL_PLSQL_VARIABLE),
            (_TYPE_RE, S.LABEL_PLSQL_TYPE),
        ):
            for m in regex.finditer(text):
                name = m.group(1).upper()
                if name in {"BEGIN", "END", "IF", "LOOP", "NULL", "RETURN"}:
                    continue
                line_no = _line_of(text, m.start())
                owner_qname, owner_label = owner_at(m.start())
                scope = (
                    owner_qname.rsplit(":", 1)[-1]
                    if owner_label != S.LABEL_SOURCE_FILE
                    else normalize_repository_path(context.relative_source_path or Path(file_path).name).upper()
                )
                qname = context.logic_qname(label, f"{scope}.{name}", pkg_schema)
                _add_unique(
                    result,
                    GraphNode(
                        label=label,
                        key="qualified_name",
                        key_value=qname,
                        properties={
                            "qualified_name": qname,
                            "name": name,
                            "schema": pkg_schema or None,
                            "db_name": context.db_name,
                            "declaration": re.sub(r"\s+", " ", (m.group(2) or "").strip()).upper(),
                            "value": (m.group(3) or "").strip() if label == S.LABEL_PLSQL_VARIABLE else "",
                            "package": pkg_name or "",
                            "service": service,
                            "repository": repository,
                            "source_file": file_path,
                            "line": line_no + 1,
                            "layer": "logic",
                        },
                    ),
                )
                result.edges.append(
                    GraphEdge(
                        label,
                        "qualified_name",
                        qname,
                        owner_label,
                        "qualified_name",
                        owner_qname,
                        S.REL_BELONGS_TO,
                        {"line": line_no + 1, "source_file": file_path},
                    )
                )

        # ── SQL DML scan ─────────────────────────────────────────────────────
        # Collect table-level operations; column names stay edge metadata, not graph nodes.
        ops: list[tuple[int, int, str, str, list[str]]] = []

        for m in _SQL_INSERT.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        m.start(),
                        t,
                        "INSERT",
                        _insert_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_UPDATE.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        m.start(),
                        t,
                        "UPDATE",
                        _update_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_DELETE.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append((_line_of(text, m.start()), m.start(), t, "DELETE", []))

        for m in _SQL_MERGE.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        m.start(),
                        t,
                        "MERGE",
                        _merge_columns(scan_text, m.end()),
                    )
                )

        for m in _SQL_FROM.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        m.start(),
                        t,
                        "SELECT",
                        _read_columns(scan_text, m.start(), m.end()),
                    )
                )

        for m in _SQL_JOIN.finditer(scan_text):
            t = _resolve_synonym(_norm(m.group(1)))
            if not _skip(t):
                ops.append(
                    (
                        _line_of(text, m.start()),
                        m.start(),
                        t,
                        "SELECT",
                        _read_columns(scan_text, m.start(), m.end()),
                    )
                )

        # EXECUTE IMMEDIATE with string literal — parse the embedded SQL
        for m in _EXEC_IMMEDIATE.finditer(text):
            for t, op in _tables_from_sql_literal(m.group(1)):
                ops.append((_line_of(text, m.start()), m.start(), t, op, []))
        dynamic_sql_vars = _dynamic_sql_literals(text)
        for m in _EXEC_IMMEDIATE_VAR.finditer(text):
            for t, op in _tables_from_sql_literal(dynamic_sql_vars.get(m.group(1).upper(), "")):
                ops.append((_line_of(text, m.start()), m.start(), t, op, []))

        # ── Assign each op to its bounded lexical owner ──────────────────────
        seen_edges: set[tuple[str, str, str, int, str]] = set()
        for line_no, pos, table_name, op, columns in ops:
            owner_qname, owner_label = owner_at(pos)
            schema, object_name, unresolved = context.resolved_object(table_name)
            tbl_qname = context.table_qname(table_name)
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_TABLE,
                    key="qualified_name",
                    key_value=tbl_qname,
                    properties={
                        "qualified_name": tbl_qname,
                        "name": object_name,
                        "schema": schema,
                        "schema_unresolved": unresolved,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )
            rel = _rel_type(op)
            edge_key = (owner_qname, tbl_qname, rel, line_no + 1, op)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            result.edges.append(
                GraphEdge(
                    owner_label,
                    "qualified_name",
                    owner_qname,
                    S.LABEL_TABLE,
                    "qualified_name",
                    tbl_qname,
                    rel,
                    {
                        "operation": op,
                        "columns": columns,
                        "line": line_no + 1,
                        "source_file": file_path,
                    },
                )
            )

        for m in _SEQ_USAGE.finditer(scan_text):
            seq_name = _norm(m.group(1))
            if _skip(seq_name):
                continue
            line_no = _line_of(text, m.start())
            owner_qname, owner_label = owner_at(m.start())
            schema, object_name, unresolved = context.resolved_object(seq_name)
            seq_qname = context.sequence_qname(seq_name)
            _add_unique(
                result,
                GraphNode(
                    label=S.LABEL_SEQUENCE,
                    key="qualified_name",
                    key_value=seq_qname,
                    properties={
                        "qualified_name": seq_qname,
                        "name": object_name,
                        "schema": schema,
                        "schema_unresolved": unresolved,
                        "repository": repository,
                        "db_name": context.db_name,
                        "layer": "data",
                    },
                ),
            )
            edge_key = (
                owner_qname,
                seq_qname,
                S.REL_USES_SEQUENCE,
                line_no + 1,
                "SEQUENCE",
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            result.edges.append(
                GraphEdge(
                    owner_label,
                    "qualified_name",
                    owner_qname,
                    S.LABEL_SEQUENCE,
                    "qualified_name",
                    seq_qname,
                    S.REL_USES_SEQUENCE,
                    {
                        "operation": "SEQUENCE",
                        "line": line_no + 1,
                        "source_file": file_path,
                    },
                )
            )

        # ── Exception handler flow ───────────────────────────────────────────
        for m in re.finditer(
            r"\bEXCEPTION\b\s+\bWHEN\b\s+(.+?)\s+\bTHEN\b",
            scan_text,
            re.IGNORECASE | re.DOTALL,
        ):
            line_no = _line_of(text, m.start())
            owner_qname, owner_label = owner_at(m.start())
            result.edges.append(
                GraphEdge(
                    owner_label,
                    "qualified_name",
                    owner_qname,
                    owner_label,
                    "qualified_name",
                    owner_qname,
                    "HANDLES_EXCEPTION",
                    {
                        "handler": re.sub(r"\s+", " ", m.group(1).strip()).upper(),
                        "line": line_no + 1,
                        "source_file": file_path,
                    },
                )
            )

        # ── Inter-package CALLS ───────────────────────────────────────────────
        seen_calls: set[tuple[str, str]] = set()
        for m in _PKG_CALL_RE.finditer(scan_text):
            call_schema = (m.group("schema") or ctx_schema).upper()
            pkg_ref = m.group("package").upper()
            proc_ref = m.group("routine").upper()
            if pkg_ref in _ORACLE_BUILTIN_PKGS or pkg_ref in _PLSQL_KW_PREFIXES:
                continue
            if _skip(pkg_ref) or _skip(proc_ref):
                continue
            if pkg_name and pkg_ref == pkg_name:
                continue
            call_line = _line_of(text, m.start())
            caller_qname, caller_label = owner_at(m.start())
            target_qname = context.logic_qname(
                S.LABEL_PROCEDURE, f"{pkg_ref}.{proc_ref}", call_schema
            )
            edge_key = (caller_qname, target_qname)
            if edge_key in seen_calls:
                continue
            seen_calls.add(edge_key)
            result.edges.append(
                GraphEdge(
                    caller_label,
                    "qualified_name",
                    caller_qname,
                    S.LABEL_PROCEDURE,
                    "qualified_name",
                    target_qname,
                    S.REL_CALLS,
                    {
                        "call_type": "package_proc",
                        "line": call_line + 1,
                        "source_file": file_path,
                    },
                )
            )

        return result


# ── Helpers ───────────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    """Strip quotes/space, preserve schema and dblink."""
    raw = name.strip()
    if '"' in raw:
        parts = [part.strip() for part in re.split(r"\s*\.\s*", raw)]
        normalized = []
        for part in parts:
            if part.startswith('"') and part.endswith('"'):
                normalized.append(part)
            else:
                normalized.append(part.replace('"', "").upper())
        return ".".join(normalized)
    return re.sub(r"\s+", "", raw).upper()


def _parse_parameters(raw: str) -> list[dict[str, str]]:
    if not raw.strip():
        return []
    body = raw.strip()[1:-1] if raw.strip().startswith("(") and raw.strip().endswith(")") else raw
    params = []
    for part in _split_top_level(body):
        tokens = re.sub(r"\s+", " ", part.strip()).split(" ")
        if len(tokens) < 2:
            continue
        name = _norm(tokens[0])
        mode = ""
        type_start = 1
        if tokens[1].upper() in {"IN", "OUT", "INOUT"}:
            mode = tokens[1].upper()
            type_start = 2
            if len(tokens) > 2 and tokens[2].upper() == "OUT":
                mode = "IN OUT"
                type_start = 3
        params.append({"name": name, "mode": mode or "IN", "data_type": " ".join(tokens[type_start:]).upper()})
    return params


def _resolve_synonym(name: str) -> str:
    """Resolve sample/local synonyms until a synonym catalog is available."""
    return {"EMP_SYN": "EMP"}.get(name, name)


def _skip(name: str) -> bool:
    if not name or len(name) < 2:
        return True
    if name in _SKIP_NAMES:
        return True
    if bool(_SYS_PREFIX_RE.match(name)):
        return True
    if name.startswith("("):
        return True
    return False


def _line_of(text: str, pos: int) -> int:
    return text[:pos].count("\n")


def _mask_string_literals(text: str) -> str:
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            out.append(" ")
            if in_str and i + 1 < len(text) and text[i + 1] == "'":
                out.append(" ")
                i += 2
                continue
            in_str = not in_str
        else:
            out.append("\n" if ch == "\n" else (" " if in_str else ch))
        i += 1
    return "".join(out)




def _resolve_active(spans: list[tuple[int, int, str]], pos: int) -> str | None:
    active = [item for item in spans if item[0] <= pos <= item[1]]
    if not active:
        return None
    return max(active, key=lambda item: item[0])[2]


def _routine_declarations(
    text: str,
    context: ExtractionContext,
    package_name: str,
    package_schema: str,
) -> list[_RoutineDeclaration]:
    candidates = [
        (match.start(), match, label)
        for regex, label in (
            (_PROC_RE, S.LABEL_PROCEDURE),
            (_FUNC_RE, S.LABEL_SQL_FUNCTION),
        )
        for match in regex.finditer(text)
    ]
    declarations: list[_RoutineDeclaration] = []
    spans: list[tuple[int, int, str]] = []
    for _, match, label in sorted(candidates, key=lambda item: item[0]):
        name = match.group("name").upper()
        parent_qname = _resolve_active(spans, match.start())
        parent_name = parent_qname.rsplit(":", 1)[-1] if parent_qname else ""
        full_name = (
            f"{parent_name}.{name}"
            if parent_name
            else f"{package_name}.{name}" if package_name else name
        )
        schema = (match.group("schema") or package_schema).upper()
        declaration = _RoutineDeclaration(
            start=match.start(),
            end=_routine_end(text, match.start()),
            qname=context.logic_qname(label, full_name, schema),
            label=label,
            name=name,
            schema=schema,
            match=match,
        )
        declarations.append(declaration)
        spans.append((declaration.start, declaration.end, declaration.qname))
    return declarations


def _routine_end(text: str, start: int) -> int:
    masked = _mask_comments(_mask_string_literals(text))
    declaration = (
        r"\b(?P<kind>PROCEDURE|FUNCTION|TRIGGER)\s+"
        r'(?:"?[\w$#]+"?\s*\.\s*)?"?(?P<name>[\w$#]+)"?'
    )
    token_re = re.compile(
        declaration
        + r"|\b(?P<begin>BEGIN)\b"
        + r"|\bEND(?:\s+(?:PROCEDURE\s+|FUNCTION\s+)?(?P<end_name>[\w$#]+))?\s*;",
        re.IGNORECASE,
    )
    stack: list[dict[str, object]] = []
    for match in token_re.finditer(masked, start):
        if match.group("kind"):
            kind = match.group("kind").upper()
            header_end = re.search(
                r"\b(?:IS|AS|BEGIN)\b|;", masked[match.end() :], re.IGNORECASE
            )
            if kind != "TRIGGER" and header_end and header_end.group(0) == ";":
                if not stack:
                    return match.end() + header_end.end()
                continue
            stack.append({"kind": "routine", "begun": False})
            continue
        if match.group("begin"):
            if stack and stack[-1]["kind"] == "routine" and not stack[-1]["begun"]:
                stack[-1]["begun"] = True
            elif stack:
                stack.append({"kind": "block", "begun": True})
            continue
        if (match.group("end_name") or "").upper() in {"IF", "LOOP", "CASE"}:
            continue
        if stack:
            stack.pop()
            if not stack:
                return match.end()
    return len(text)


def _mask_comments(text: str) -> str:
    text = re.sub(
        r"/\*.*?\*/",
        lambda match: "\n" * match.group(0).count("\n"),
        text,
        flags=re.DOTALL,
    )
    return re.sub(r"--[^\n]*", lambda match: " " * len(match.group(0)), text)


def _rel_type(op: str) -> str:
    return {
        "INSERT": S.REL_INSERTS_INTO,
        "UPDATE": S.REL_UPDATES,
        "DELETE": S.REL_DELETES_FROM,
        "MERGE": S.REL_MERGES_INTO,
    }.get(op, S.REL_READS_FROM)


def _insert_columns(text: str, pos: int) -> list[str]:
    chunk = text[pos : pos + 2_000]
    m = re.match(r"\s*\(([^)]*)\)", chunk, re.DOTALL)
    return _clean_columns(m.group(1).split(",")) if m else []


def _update_columns(text: str, pos: int) -> list[str]:
    chunk = re.split(
        r"\bWHERE\b|\bRETURNING\b|;", text[pos : pos + 4_000], 1, re.IGNORECASE
    )[0]
    return _clean_columns(part.split("=", 1)[0] for part in _split_top_level(chunk))


def _merge_columns(text: str, pos: int) -> list[str]:
    m = re.search(
        r"\bUPDATE\s+SET\s+(.+?)(?:\bWHEN\b|;)",
        text[pos : pos + 8_000],
        re.IGNORECASE | re.DOTALL,
    )
    return (
        _clean_columns(part.split("=", 1)[0] for part in _split_top_level(m.group(1)))
        if m
        else []
    )


def _read_columns(text: str, table_start: int, table_end: int) -> list[str]:
    alias = _table_alias(text, table_end)
    before = text[max(0, table_start - 4_000) : table_start]
    select_pos = before.upper().rfind("SELECT")
    select_list = before[select_pos + 6 :] if select_pos >= 0 else ""
    after = re.split(
        r"\bJOIN\b|\bGROUP\b|\bORDER\b|\bHAVING\b|\bLOOP\b|;",
        text[table_end : table_end + 2_000],
        1,
        re.IGNORECASE | re.DOTALL,
    )[0]
    if alias:
        return _clean_columns(
            m.group(1)
            for m in re.finditer(
                rf"\b{re.escape(alias)}\s*\.\s*([\w$#]+)\b",
                select_list + " " + after,
                re.IGNORECASE,
            )
        )
    return _select_columns(select_list)


def _table_alias(text: str, pos: int) -> str:
    m = re.match(
        r"\s+(?!WHERE\b|JOIN\b|ON\b|GROUP\b|ORDER\b|HAVING\b|LOOP\b|CONNECT\b)(?:AS\s+)?\"?([\w$#]+)\"?",
        text[pos : pos + 80],
        re.IGNORECASE,
    )
    return _norm(m.group(1)) if m else ""


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = depth = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def _clean_columns(items) -> list[str]:
    cols: list[str] = []
    for item in items:
        col = _norm(str(item).split(".")[-1])
        if col and not _skip(col) and col not in cols:
            cols.append(col)
    return cols


_SQL_FUNCTION_NAMES = {
    "COUNT",
    "SUM",
    "AVG",
    "MIN",
    "MAX",
    "NVL",
    "COALESCE",
    "ROUND",
    "TRUNC",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "DECODE",
    "CASE",
    "SYSDATE",
    "SYSTIMESTAMP",
}


def _select_columns(select_list: str) -> list[str]:
    cols: list[str] = []
    select_list = re.split(r"\bINTO\b", select_list, 1, re.IGNORECASE)[0]
    for expr in _split_top_level(select_list):
        if "*" in expr and not re.search(r"\.\s*\*", expr):
            continue
        for fn in _SQL_FUNCTION_NAMES:
            expr = re.sub(rf"\b{fn}\s*\(", "(", expr, flags=re.IGNORECASE)
        matches = re.findall(r"(?:\b[\w$#]+\s*\.\s*)?\b([\w$#]+)\b", expr)
        if not matches:
            continue
        candidate = _norm(matches[0] if len(matches) == 1 else matches[-2])
        if candidate and not _skip(candidate) and candidate not in cols:
            cols.append(candidate)
    return cols


def _add_unique(result: ExtractionResult, node: GraphNode) -> None:
    if node.key_value not in {n.key_value for n in result.nodes}:
        result.nodes.append(node)


# Mini SQL scanners for EXECUTE IMMEDIATE string content
_MINI_INSERT = re.compile(
    r"\bINSERT\s+(?:INTO\s+)?(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE
)
_MINI_UPDATE = re.compile(r"\bUPDATE\s+(?:[\w$#]+\.)?([\w$#]+)\s+SET\b", re.IGNORECASE)
_MINI_DELETE = re.compile(r"\bDELETE\s+FROM\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)
_MINI_MERGE = re.compile(r"\bMERGE\s+INTO\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)
_MINI_FROM = re.compile(r"\bFROM\s+(?:[\w$#]+\.)?([\w$#]+)", re.IGNORECASE)


def _tables_from_sql_literal(sql: str) -> list[tuple[str, str]]:
    results = []
    for m in _MINI_INSERT.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "INSERT"))
    for m in _MINI_UPDATE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "UPDATE"))
    for m in _MINI_DELETE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "DELETE"))
    for m in _MINI_MERGE.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "MERGE"))
    for m in _MINI_FROM.finditer(sql):
        t = _norm(m.group(1))
        if not _skip(t):
            results.append((t, "SELECT"))
    return results

def _dynamic_sql_literals(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _DYNAMIC_SQL_ASSIGN_RE.finditer(text):
        parts = re.findall(r"'([^']*)'", m.group(2))
        if parts:
            out[m.group(1).upper()] = "".join(parts)
    return out
