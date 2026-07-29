"""Dependency-free helpers implementing stable graph identities and validation."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

NODE_TYPES = frozenset({
    "DATABASE", "TABLE", "SCREEN", "UI_ACTION", "API_APPLICATION", "API_OPERATION",
    "JOB_NETWORK", "JOB", "EXECUTABLE", "COMMAND_MODE", "PLSQL_PACKAGE", "PROCEDURE",
    "FUNCTION", "TRIGGER", "VIEW", "MATERIALIZED_VIEW", "SQL_FILE", "EXTERNAL_SYSTEM",
    "EXTERNAL_API_OPERATION", "EXTERNAL_DATABASE_OBJECT", "UNRESOLVED_REFERENCE",
    "ANGULAR_PROJECT", "ANGULAR_COMPONENT", "ANGULAR_SERVICE", "API_CALL_REFERENCE",
    "DOTNET_SOLUTION", "DOTNET_PROJECT", "CSHARP_TYPE", "CONTROLLER", "SERVICE", "REPOSITORY",
    "EXECUTABLE_ENTRY_POINT", "LOCAL_ROUTINE", "METHOD", "INLINE_SQL", "SEQUENCE", "SYNONYM", "DATABASE_LINK",
    "COLUMN", "FILE", "SYSTEM", "APPLICATION", "DATA_FILE",
    "XML_SQL_MAPPER", "MAPPER_STATEMENT", "XML_SQL_FRAGMENT",
    # v2 knowledge contract names. Legacy producer names above remain valid so
    # existing extractor packages can be merged during the additive migration.
    "PROJECT", "ASSEMBLY", "NAMESPACE", "COMPONENT", "ROUTE",
    "API_CLIENT_CALL", "CONFIG_KEY", "CLASS", "INTERFACE", "PACKAGE",
    "SQL_STATEMENT", "LOADER_CONTROL",
})
EDGE_TYPES = frozenset({
    "CALLS_API", "STARTS", "CALLS", "READS", "REMOTE_READS", "INSERTS", "UPDATES",
    "DELETES", "MERGES", "WRITES", "READS_COLUMN", "WRITES_COLUMN", "DERIVES_FROM",
    "POPULATES", "TRIGGERS", "DEPENDS_ON", "USES", "CONTAINS", "HANDLED_BY",
    "ENTRY_IN", "RESOLVES_TO", "NAVIGATES_TO", "BELONGS_TO", "PROJECT_REFERENCE",
    "DEFINES_STATEMENT", "INCLUDES_FRAGMENT",
    # v2 semantic names. The legacy data-flow verbs are retained as aliases.
    "IMPLEMENTS", "INJECTS", "RETURNS", "THROWS", "ROUTES_TO", "HANDLES_API",
    "READS_FROM", "WRITES_TO", "USES_SEQUENCE", "EXECUTES_SQL", "LOADS_INTO",
    "MAPS_TO", "TRIGGERS_JOB",
})
GRAPH_LAYERS = frozenset({"STRUCTURAL", "TECHNICAL", "DATA_FLOW"})
GRAPH_ROLES = frozenset({"MAIN", "TECHNICAL", "EVIDENCE"})
TARGET_TYPES = frozenset({"NODE", "EDGE"})
ISSUE_TYPES = frozenset(
    "TABLE_NOT_IMPORTED COLUMN_NOT_IMPORTED PROCEDURE_NOT_FOUND "
    "EXECUTABLE_NOT_MAPPED API_ROUTE_NOT_MATCHED API_ROUTE_AMBIGUOUS "
    "AMBIGUOUS_API_LINK AMBIGUOUS_DB_LINK AMBIGUOUS_MAPPER_LINK "
    "AMBIGUOUS_SYMBOL DYNAMIC_SQL DYNAMIC_CONFIG_KEY EXTERNAL_OBJECT "
    "PARSE_ERROR INVALID_CONFIG ENCODING_ERROR ENCODING_CONFLICT "
    "MERGE_CONFLICT DUPLICATE_MAPPER_STATEMENT "
    "AUTO_DISCOVERED_XML_SQL_TAG UNRESOLVED_XML_INCLUDE "
    "UNRESOLVED_REFERENCE SEMANTIC_TREE_UNAVAILABLE FILE_TOO_LARGE TIMEOUT "
    "FALLBACK_USED NO_API_ENDPOINTS".split()
)

NODE_ID_PREFIXES = {
    "DATABASE": frozenset({"database"}),
    "TABLE": frozenset({"table"}),
    "COLUMN": frozenset({"column"}),
    "SCREEN": frozenset({"screen"}),
    "UI_ACTION": frozenset({"ui-action"}),
    "API_APPLICATION": frozenset({"api-application"}),
    "API_OPERATION": frozenset({"api-operation"}),
    "JOB_NETWORK": frozenset({"job-network"}),
    "JOB": frozenset({"job"}),
    "EXECUTABLE": frozenset({"executable"}),
    "COMMAND_MODE": frozenset({"command-mode"}),
    "PLSQL_PACKAGE": frozenset({"plsql-package"}),
    "PROCEDURE": frozenset({"procedure"}),
    "FUNCTION": frozenset({"function"}),
    "TRIGGER": frozenset({"trigger"}),
    "VIEW": frozenset({"view"}),
    "MATERIALIZED_VIEW": frozenset({"materialized-view"}),
    "SQL_FILE": frozenset({"sql-file"}),
    "EXTERNAL_SYSTEM": frozenset({"external-system"}),
    "EXTERNAL_API_OPERATION": frozenset({"external-api"}),
    "EXTERNAL_DATABASE_OBJECT": frozenset({"external-db-object"}),
    "UNRESOLVED_REFERENCE": frozenset({"unresolved-reference"}),
    "ANGULAR_PROJECT": frozenset({"angular-project"}),
    "ANGULAR_COMPONENT": frozenset({"angular-component"}),
    "ANGULAR_SERVICE": frozenset({"angular-service"}),
    "API_CALL_REFERENCE": frozenset({"api-call"}),
    "DOTNET_SOLUTION": frozenset({"dotnet-solution"}),
    "DOTNET_PROJECT": frozenset({"dotnet-project"}),
    "CSHARP_TYPE": frozenset({"csharp-type"}),
    "CONTROLLER": frozenset({"controller"}),
    "SERVICE": frozenset({"service"}),
    "REPOSITORY": frozenset({"repository"}),
    "EXECUTABLE_ENTRY_POINT": frozenset({"executable-entry", "executable-entry-point"}),
    "LOCAL_ROUTINE": frozenset({"local-routine", "procedure", "function"}),
    "METHOD": frozenset({"method"}),
    "INLINE_SQL": frozenset({"inline-sql"}),
    "SEQUENCE": frozenset({"sequence"}),
    "SYNONYM": frozenset({"synonym"}),
    "DATABASE_LINK": frozenset({"database-link"}),
    "FILE": frozenset({"file"}),
    "SYSTEM": frozenset({"system"}),
    "APPLICATION": frozenset({"application"}),
    "DATA_FILE": frozenset({"data-file"}),
    "XML_SQL_MAPPER": frozenset({"xml-sql-mapper"}),
    "MAPPER_STATEMENT": frozenset({"mapper-statement"}),
    "XML_SQL_FRAGMENT": frozenset({"xml-sql-fragment"}),
    "PROJECT": frozenset({"project", "angular-project", "dotnet-project"}),
    "ASSEMBLY": frozenset({"assembly"}),
    "NAMESPACE": frozenset({"namespace"}),
    "COMPONENT": frozenset({"component", "angular-component"}),
    "ROUTE": frozenset({"route", "screen"}),
    "API_CLIENT_CALL": frozenset({"api-client-call", "api-call"}),
    "CONFIG_KEY": frozenset({"config-key"}),
    "CLASS": frozenset({"class", "csharp-type"}),
    "INTERFACE": frozenset({"interface", "csharp-type"}),
    "PACKAGE": frozenset({"package", "plsql-package"}),
    "SQL_STATEMENT": frozenset({"sql-statement", "inline-sql"}),
    "LOADER_CONTROL": frozenset({"loader-control", "sql-file"}),
}

_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")
_ROUTE_PARAM = re.compile(r"(?:\{[^/{}]+\}|<(?:(?:[^:>/]+):)?[^/>]+>|:[A-Za-z_][A-Za-z0-9_]*)")
_ID_PART = re.compile(r"^[^|\r\n]+$")
_STABLE_ID = re.compile(r"^[a-z][a-z0-9-]*(?::[^|\r\n:][^|\r\n]*)+$")
_UUID_PART = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")

def normalize_repository_path(value: str) -> str:
    """Return a canonical repository-relative path or reject traversal/absolute paths."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("repository path must be a non-empty string")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or _WINDOWS_ABSOLUTE.match(value.strip()):
        raise ValueError("repository path must be relative")
    parts = raw.split("/")
    if any(part == ".." for part in parts):
        raise ValueError("repository path cannot contain '..'")
    normalized = str(PurePosixPath(*[part for part in parts if part not in ("", ".")]))
    if normalized in ("", "."):
        raise ValueError("repository path must name a file or directory")
    return normalized

def normalize_http_route(method: str, route: str) -> tuple[str, str]:
    """Normalize an HTTP method and route without retaining environment host data."""
    normalized_method = str(method or "").strip().upper()
    if not normalized_method or not re.fullmatch(r"[A-Z]+", normalized_method):
        raise ValueError("HTTP method must contain letters")
    raw = str(route or "").strip()
    if not raw:
        raise ValueError("HTTP route must be non-empty")
    parsed = urlsplit(raw if "://" in raw or raw.startswith("//") else "//contract.local/" + raw.lstrip("/"))
    path = parsed.path or "/"
    path = re.sub(r"/+", "/", path)
    path = _ROUTE_PARAM.sub("{id}", path)
    if path != "/":
        path = path.rstrip("/")
    return normalized_method, path

def normalize_oracle_identifier(value: str) -> str:
    """Uppercase unquoted Oracle identifier parts; preserve quoted part case."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Oracle identifier must be non-empty")
    parts: list[str] = []
    token = ""
    quoted = False
    index = 0
    text = value.strip()
    while index < len(text):
        char = text[index]
        if char == '"':
            token += char
            if quoted and index + 1 < len(text) and text[index + 1] == '"':
                token += '"'
                index += 1
            else:
                quoted = not quoted
        elif char == "." and not quoted:
            parts.append(_normalize_oracle_part(token))
            token = ""
        else:
            token += char
        index += 1
    if quoted:
        raise ValueError("unterminated quoted Oracle identifier")
    parts.append(_normalize_oracle_part(token))
    return ".".join(parts)

def _normalize_oracle_part(value: str) -> str:
    part = value.strip()
    if not part:
        raise ValueError("Oracle identifier contains an empty part")
    if part.startswith('"') and part.endswith('"'):
        return part[1:-1].replace('""', '"')
    if '"' in part:
        raise ValueError("invalid quoted Oracle identifier")
    return part.upper()

def stable_node_id(kind: str, *parts: str) -> str:
    """Build any required colon-delimited stable node ID from canonical parts."""
    prefix = str(kind or "").strip().lower().replace("_", "-")
    values = [str(part).strip() for part in parts]
    if not prefix or not values or any(not value or not _ID_PART.fullmatch(value) for value in values):
        raise ValueError("stable node ID requires non-empty canonical parts")
    return ":".join((prefix, *values))

def database_id(database: str) -> str:
    return stable_node_id("database", normalize_oracle_identifier(database))

def table_id(database: str, table_code: str) -> str:
    return stable_node_id("table", normalize_oracle_identifier(database), normalize_oracle_identifier(table_code))

def column_id(database: str, table_code: str, column_code: str) -> str:
    return stable_node_id("column", normalize_oracle_identifier(database), normalize_oracle_identifier(table_code), normalize_oracle_identifier(column_code))

def api_operation_id(application: str, method: str, route: str) -> str:
    verb, path = normalize_http_route(method, route)
    return stable_node_id("api-operation", application, verb, path)

def sql_file_id(repository: str, relative_path: str) -> str:
    return stable_node_id("sql-file", repository, normalize_repository_path(relative_path))

def routine_id(kind: str, database: str, owner: str, name: str, parameter_types: tuple[str, ...] = ()) -> str:
    if kind.upper() not in {"PROCEDURE", "FUNCTION"}:
        raise ValueError("routine kind must be PROCEDURE or FUNCTION")
    signature = ",".join(normalize_oracle_identifier(item) for item in parameter_types) or "void"
    return stable_node_id(kind, normalize_oracle_identifier(database), normalize_oracle_identifier(owner), normalize_oracle_identifier(name), signature)

def canonical_edge_id(source_node_id: str, edge_type: str, target_node_id: str, raw_operation: str, graph_layer: str) -> str:
    """Hash only the contract's canonical edge identity tuple."""
    validate_enum("edge_type", edge_type, EDGE_TYPES)
    validate_enum("graph_layer", graph_layer, GRAPH_LAYERS)
    fields = (source_node_id, edge_type, target_node_id, raw_operation or "", graph_layer)
    if any(not isinstance(field, str) or "|" in field or "\n" in field or "\r" in field for field in fields):
        raise ValueError("edge identity fields must be strings without delimiters")
    if not source_node_id or not target_node_id:
        raise ValueError("edge endpoints must be non-empty")
    digest = hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()
    return f"edge:{digest}"

def canonical_evidence_key(row: dict[str, str]) -> tuple[str, ...]:
    """Return the evidence dedupe key, excluding ID, snippet, confidence and properties."""
    return (
        row.get("target_type", ""),
        row.get("target_id", ""),
        row.get("source_path", ""),
        row.get("start_line", ""),
        row.get("end_line", ""),
        row.get("start_column", ""),
        row.get("end_column", ""),
        row.get("evidence_kind", ""),
        row.get("extractor_name", ""),
    )

def validate_stable_id(value: str, node_type: str | None = None) -> str:
    """Validate stable ID syntax and, when known, the node-type prefix contract."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stable ID must be a non-empty string")
    if value != value.strip() or not _STABLE_ID.fullmatch(value):
        raise ValueError(f"invalid stable ID: {value!r}")
    if "://" in value or "\\" in value:
        raise ValueError("stable ID cannot contain host URLs or backslashes")
    parts = value.split(":")
    if any(part == "" for part in parts):
        raise ValueError("stable ID cannot contain empty identity parts")
    if any(_UUID_PART.fullmatch(part) for part in parts[1:]):
        raise ValueError("stable ID cannot contain UUID identity parts")
    if node_type is not None:
        validate_enum("node_type", node_type, NODE_TYPES)
        prefixes = NODE_ID_PREFIXES.get(node_type, frozenset())
        if prefixes and parts[0] not in prefixes:
            raise ValueError(f"node_id prefix {parts[0]!r} does not match node_type {node_type!r}")
        _validate_type_specific_stable_id(value, node_type)
    return value

def _validate_type_specific_stable_id(value: str, node_type: str) -> None:
    prefix = value.split(":", 1)[0]
    if node_type in {"API_OPERATION", "API_CALL_REFERENCE", "EXTERNAL_API_OPERATION"}:
        parts = value.split(":", 3)
        if len(parts) != 4:
            raise ValueError(f"{node_type} ID must include system, HTTP method and route")
        method, route = normalize_http_route(parts[2], parts[3])
        if parts[2] != method or parts[3] != route:
            raise ValueError(f"{node_type} ID HTTP route is not canonical")
    elif node_type in {"PROCEDURE", "FUNCTION"} or (node_type == "LOCAL_ROUTINE" and prefix in {"procedure", "function"}):
        parts = value.split(":")
        if len(parts) != 5:
            raise ValueError(f"{node_type} routine ID must include database, package/name, routine name and signature")
    elif node_type == "SCREEN":
        parts = value.split(":", 2)
        if len(parts) != 3:
            raise ValueError("SCREEN ID must include application and route")
        _, route = normalize_http_route("GET", parts[2])
        if parts[2] != route:
            raise ValueError("SCREEN ID route is not canonical")
    elif node_type == "SQL_FILE":
        parts = value.split(":", 2)
        if len(parts) != 3 or normalize_repository_path(parts[2]) != parts[2]:
            raise ValueError("SQL_FILE ID must include a canonical repository-relative path")
    elif node_type in {"DOTNET_PROJECT", "DOTNET_SOLUTION"}:
        parts = value.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"{node_type} ID must include repository and relative path/name")
        if "/" in parts[2] or "\\" in parts[2]:
            normalize_repository_path(parts[2])

def validate_evidence_location(
    source_path: str,
    start_line: str,
    end_line: str,
    start_column: str,
    end_column: str,
) -> str:
    """Validate evidence path and line/column range, returning canonical source_path."""
    path = normalize_repository_path(source_path)
    start = _optional_positive_int("start_line", start_line)
    end = _optional_positive_int("end_line", end_line)
    start_col = _optional_positive_int("start_column", start_column)
    end_col = _optional_positive_int("end_column", end_column)
    if (start is None) != (end is None):
        raise ValueError("evidence start_line and end_line must be both present or both empty")
    if (start_col is None) != (end_col is None):
        raise ValueError("evidence start_column and end_column must be both present or both empty")
    if start is not None and end is not None and end < start:
        raise ValueError("evidence end_line cannot precede start_line")
    if start is not None and end == start and start_col is not None and end_col is not None and end_col < start_col:
        raise ValueError("evidence end_column cannot precede start_column on the same line")
    return path

def _optional_positive_int(name: str, value: str) -> int | None:
    if value == "" or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed

def validate_enum(name: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {name}: {value!r}")
    return value

def validate_confidence(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("confidence must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be numeric") from exc
    if not 0.0 <= result <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    return result

def validate_properties_json(value: str | dict[str, object]) -> dict[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError("properties_json must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("properties_json must be a JSON object")
    return parsed

def validate_node_fields(node_type: str, graph_role: str, confidence: object, properties_json: str | dict[str, object]) -> None:
    validate_enum("node_type", node_type, NODE_TYPES)
    validate_enum("graph_role", graph_role, GRAPH_ROLES)
    validate_confidence(confidence)
    validate_properties_json(properties_json)

def validate_edge_fields(edge_type: str, graph_layer: str, confidence: object, properties_json: str | dict[str, object]) -> None:
    validate_enum("edge_type", edge_type, EDGE_TYPES)
    validate_enum("graph_layer", graph_layer, GRAPH_LAYERS)
    validate_confidence(confidence)
    validate_properties_json(properties_json)
