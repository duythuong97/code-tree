from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from contract.graph_contract import (
    api_operation_id,
    canonical_edge_id,
    canonical_evidence_key,
    column_id,
    database_id,
    normalize_http_route,
    normalize_oracle_identifier,
    normalize_repository_path,
    sql_file_id,
    stable_node_id,
    table_id,
)

CSV_HEADERS = {
    "nodes": "node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json".split(","),
    "edges": "edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json".split(","),
    "evidence": "evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json".split(","),
    "issues": "issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json".split(","),
}
OPTIONAL_CSV_HEADERS = {
    "localized_texts": "target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at".split(","),
}
PACKAGE_CSV_HEADERS = {**CSV_HEADERS, **OPTIONAL_CSV_HEADERS}

EDGE_MAP = {
    "SELECT": "READS",
    "READ": "READS",
    "INSERT": "INSERTS",
    "UPDATE": "UPDATES",
    "DELETE": "DELETES",
    "MERGE": "MERGES",
    "UPSERT": "WRITES",
    "CALL": "CALLS",
}

@dataclass(frozen=True)
class SourceFile:
    absolute: Path
    relative: str
    text: str
    database: str = ""

class Catalog:
    def __init__(self, tables: dict[str, set[str]], procedures: set[str] | None = None) -> None:
        self.tables = tables
        self.procedures = procedures or set()

    @classmethod
    def load(cls, input_root: Path, database: str = "") -> "Catalog":
        tables: dict[str, dict[str, set[str]]] = {}
        path = input_root / "tables.csv"
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    db = normalize_oracle_identifier(row["database"])
                    if database and db != normalize_oracle_identifier(database):
                        continue
                    table = normalize_oracle_identifier(row["table_code"])
                    columns: set[str] = set()
                    child = input_root / "tables" / f"{row['table_code']}.csv"
                    if child.exists():
                        with child.open(encoding="utf-8", newline="") as column_handle:
                            columns = {normalize_oracle_identifier(item["column_code"]) for item in csv.DictReader(column_handle)}
                    tables.setdefault(db, {})[table] = columns
        return cls(tables)  # type: ignore[arg-type]

    def has_table(self, database: str, table: str) -> bool:
        db = normalize_oracle_identifier(database)
        name = leaf_identifier(table)
        return name in self.tables.get(db, set())

    def has_column(self, database: str, table: str, column: str) -> bool:
        db = normalize_oracle_identifier(database)
        name = leaf_identifier(table)
        col = normalize_oracle_identifier(column)
        values = self.tables.get(db, {})
        if isinstance(values, dict):
            return col in values.get(name, set())
        return False


def leaf_identifier(name: str) -> str:
    raw = name.split("@", 1)[0].split(".")[-1]
    return normalize_oracle_identifier(raw.strip('"'))


def owner_identifier(name: str, default_owner: str) -> str:
    clean = name.split("@", 1)[0]
    parts = [part for part in clean.split(".") if part]
    if len(parts) >= 2:
        return normalize_oracle_identifier(parts[-2])
    return normalize_oracle_identifier(default_owner)


def to_json(value: dict | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def slug(value: str) -> str:
    text = value.strip().replace("\\", "/")
    text = text.rsplit("/", 1)[-1]
    out = []
    for char in text:
        if char.isalnum():
            out.append(char.lower())
        elif char in {"-", "_", "."}:
            out.append(char.lower())
        else:
            out.append("-")
    result = "".join(out).strip("-")
    while "--" in result:
        result = result.replace("--", "-")
    return result or "source"


def normalize_api_call_route(raw_route: str) -> str:
    route = raw_route.strip().strip("'\"")
    if not route:
        return "/"
    method = "GET"
    if " " in route and route.split(" ", 1)[0].isalpha():
        method, route = route.split(" ", 1)
    _, path = normalize_http_route(method, route)
    return path


def api_call_id(source: str, method: str, route: str) -> str:
    verb, path = normalize_http_route(method, route)
    return stable_node_id("api-call", source, verb, path)


def procedure_id(database: str, package: str, name: str, signature: str = "void") -> str:
    return stable_node_id("procedure", normalize_oracle_identifier(database), normalize_oracle_identifier(package), normalize_oracle_identifier(name), signature)


def function_id(database: str, package: str, name: str, signature: str = "void") -> str:
    return stable_node_id("function", normalize_oracle_identifier(database), normalize_oracle_identifier(package), normalize_oracle_identifier(name), signature)


def plsql_package_id(database: str, package: str) -> str:
    return stable_node_id("plsql-package", normalize_oracle_identifier(database), normalize_oracle_identifier(package))


def trigger_id(database: str, name: str) -> str:
    return stable_node_id("trigger", normalize_oracle_identifier(database), normalize_oracle_identifier(name))


def sequence_id(database: str, name: str) -> str:
    return stable_node_id("sequence", normalize_oracle_identifier(database), normalize_oracle_identifier(name))


def synonym_id(database: str, name: str) -> str:
    return stable_node_id("synonym", normalize_oracle_identifier(database), normalize_oracle_identifier(name))


def database_link_id(database: str, name: str) -> str:
    return stable_node_id("database-link", normalize_oracle_identifier(database), normalize_oracle_identifier(name))


def unresolved_id(database: str, name: str) -> str:
    return stable_node_id("unresolved-reference", normalize_oracle_identifier(database), normalize_oracle_identifier(name))


def external_db_object_id(database: str, raw_name: str) -> str:
    return stable_node_id("external-db-object", normalize_oracle_identifier(database), raw_name.strip().upper())


class PackageBuilder:
    def __init__(
        self,
        package_id: str,
        source_id: str,
        extractor_name: str,
        extractor_version: str = "1.0.0",
        metadata: dict | None = None,
    ) -> None:
        self.package_id = package_id
        self.source_id = source_id
        self.extractor_name = extractor_name
        self.extractor_version = extractor_version
        self.metadata = metadata or {}
        self.nodes: dict[str, dict[str, str]] = {}
        self.edges: dict[str, dict[str, str]] = {}
        self.evidence: dict[str, dict[str, str]] = {}
        self.issues: dict[str, dict[str, str]] = {}
        self.localized_texts: dict[tuple[str, str, str], dict[str, str]] = {}
        self.files_scanned = 0

    def add_node(
        self,
        node_id: str,
        node_type: str,
        technical_name: str,
        qualified_name: str,
        display_name: str | None = None,
        *,
        system_key: str = "",
        database_key: str = "",
        repository_key: str = "",
        graph_role: str = "MAIN",
        confidence: float = 1.0,
        properties: dict | None = None,
    ) -> str:
        row = {
            "node_id": node_id,
            "node_type": node_type,
            "technical_name": technical_name,
            "qualified_name": qualified_name,
            "default_display_name": display_name or technical_name,
            "system_key": system_key,
            "database_key": database_key,
            "repository_key": repository_key,
            "graph_role": graph_role,
            "confidence": str(float(confidence)),
            "properties_json": to_json(properties),
        }
        self.nodes.setdefault(node_id, row)
        return node_id

    def add_edge(
        self,
        source_node_id: str,
        target_node_id: str,
        edge_type: str,
        *,
        graph_layer: str = "TECHNICAL",
        raw_operation: str = "",
        confidence: float = 1.0,
        properties: dict | None = None,
    ) -> str:
        edge_id = canonical_edge_id(source_node_id, edge_type, target_node_id, raw_operation, graph_layer)
        row = {
            "edge_id": edge_id,
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "edge_type": edge_type,
            "graph_layer": graph_layer,
            "raw_operation": raw_operation,
            "confidence": str(float(confidence)),
            "properties_json": to_json(properties),
        }
        self.edges.setdefault(edge_id, row)
        return edge_id

    def merge_edge_properties(self, edge_id: str, properties: dict) -> None:
        row = self.edges.get(edge_id)
        if not row:
            return
        current = json.loads(row["properties_json"])
        current.update(properties)
        row["properties_json"] = to_json(current)

    def add_evidence(
        self,
        target_type: str,
        target_id: str,
        source_path: str,
        start_line: int | None,
        end_line: int | None,
        evidence_kind: str,
        snippet: str,
        *,
        start_column: int | None = 1,
        end_column: int | None = None,
        confidence: float = 1.0,
        properties: dict | None = None,
    ) -> str:
        path = normalize_repository_path(source_path)
        row = {
            "evidence_id": "",
            "target_type": target_type,
            "target_id": target_id,
            "source_path": path,
            "start_line": str(start_line or ""),
            "end_line": str(end_line or start_line or ""),
            "start_column": str(start_column or ""),
            "end_column": str(end_column or end_column_from_snippet(snippet)),
            "evidence_kind": evidence_kind,
            "extractor_name": self.extractor_name,
            "confidence": str(float(confidence)),
            "snippet": snippet.strip(),
            "properties_json": to_json(properties),
        }
        identity = "|".join(canonical_evidence_key(row))
        evidence_id = "ev:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        row["evidence_id"] = evidence_id
        self.evidence.setdefault(evidence_id, row)
        return evidence_id

    def add_issue(
        self,
        issue_type: str,
        severity: str,
        message: str,
        *,
        source_node_id: str = "",
        raw_reference: str = "",
        database_key: str = "",
        source_path: str = "",
        start_line: int | None = None,
        properties: dict | None = None,
    ) -> str:
        path = normalize_repository_path(source_path) if source_path else ""
        identity = f"{issue_type}|{source_node_id}|{raw_reference}|{database_key}|{path}|{start_line or ''}|{message}"
        issue_id = "issue:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        row = {
            "issue_id": issue_id,
            "issue_type": issue_type,
            "severity": severity,
            "source_node_id": source_node_id,
            "raw_reference": raw_reference,
            "database_key": database_key,
            "source_path": path,
            "start_line": str(start_line or ""),
            "message": message,
            "properties_json": to_json(properties),
        }
        self.issues.setdefault(issue_id, row)
        return issue_id

    def add_localized_text(
        self,
        target_type: str,
        target_id: str,
        field_name: str,
        locale: str,
        value: str,
        *,
        source_kind: str = "EXTRACTED",
        review_status: str = "PENDING",
        author_name: str | None = None,
        created_at: str = "",
        updated_at: str = "",
    ) -> tuple[str, str, str]:
        row = {
            "target_type": target_type,
            "target_id": target_id,
            "field_name": field_name,
            "locale": locale,
            "value": value,
            "source_kind": source_kind,
            "review_status": review_status,
            "author_name": author_name or self.extractor_name,
            "created_at": created_at,
            "updated_at": updated_at,
        }
        key = (target_id, field_name, locale)
        self.localized_texts.setdefault(key, row)
        return key

    def write(self, output: Path) -> None:
        output.mkdir(parents=True, exist_ok=True)
        groups = {
            "nodes": sorted(self.nodes.values(), key=lambda row: row["node_id"]),
            "edges": sorted(self.edges.values(), key=lambda row: row["edge_id"]),
            "evidence": sorted(self.evidence.values(), key=lambda row: row["evidence_id"]),
            "issues": sorted(self.issues.values(), key=lambda row: row["issue_id"]),
        }
        if self.localized_texts:
            groups["localized_texts"] = sorted(self.localized_texts.values(), key=lambda row: (row["target_id"], row["field_name"], row["locale"]))
        checksums: dict[str, dict[str, object]] = {}
        for name, rows in groups.items():
            path = output / f"{name}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=PACKAGE_CSV_HEADERS[name], lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            checksums[path.name] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        source = {
            "sourceKey": self.source_id,
            "repositoryKey": str(self.metadata.get("repository") or self.metadata.get("source") or self.source_id),
        }
        revision = self.metadata.get("revision")
        if revision:
            source["revision"] = str(revision)
        manifest = {
            "contractVersion": "1.0",
            "extractor": {"name": self.extractor_name, "version": self.extractor_version},
            "source": source,
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": {name: f"{name}.csv" for name in groups},
            "statistics": {"filesScanned": self.files_scanned, **{name: len(rows) for name, rows in groups.items()}},
            "checksums": checksums,
            "metadata": self.metadata,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def end_column_from_snippet(snippet: str) -> str:
    text = snippet.splitlines()[0] if snippet else ""
    return str(max(1, len(text)))


def load_config(path: Path) -> dict:
    path = path.expanduser().resolve()
    raw = _expand_paths(json.loads(path.read_text(encoding="utf-8")))
    for key in ("root", "output", "inputData"):
        value = raw.get(key)
        if value:
            candidate = Path(value).expanduser()
            raw[key] = str((candidate if candidate.is_absolute() else path.parent / candidate).resolve())
    return raw


def _expand_paths(value):
    if isinstance(value, dict):
        return {key: _expand_paths(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_paths(item) for item in value]
    if not isinstance(value, str) or "${" not in value:
        return value
    expanded = os.path.expandvars(value)
    if "${" in expanded:
        raise ValueError(f"Unresolved environment variable in config value: {value}")
    return expanded



def workspace_relative(path: Path, base_root: Path) -> str:
    return normalize_repository_path(str(path.resolve().relative_to(base_root.resolve())))


def configured_files(config: dict, suffixes: Iterable[str]) -> list[SourceFile]:
    root = Path(config["root"]).expanduser().resolve()
    folders = config.get("folders") or ["."]
    suffix_set = {suffix.lower() for suffix in suffixes}
    path_databases: dict[Path, str] = {}
    for item in folders:
        folder = item.get("path", ".") if isinstance(item, dict) else item
        database = item.get("database", "") if isinstance(item, dict) else ""
        normalized = "." if folder == "." else normalize_repository_path(folder)
        base = root.joinpath(*PurePosixPath(normalized).parts).resolve()
        if base != root and root not in base.parents:
            raise ValueError(f"folder escapes root: {folder}")
        if base.is_file():
            path_databases.setdefault(base, database)
            continue
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffix_set and not excluded_path(path, root):
                path_databases.setdefault(path, database)
    files = []
    for path, database in sorted(path_databases.items(), key=lambda item: str(item[0])):
        files.append(SourceFile(path, workspace_relative(path, root), path.read_text(encoding="utf-8"), database))
    return files


def excluded_path(path: Path, root: Path | None = None) -> bool:
    blocked = {
        ".angular", ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
        "bin", "build", "dist", "generated", "migrations", "node_modules", "obj", "venv", "__pycache__",
    }
    scoped = path.relative_to(root) if root else path
    lowered = {part.lower() for part in scoped.parts}
    name = path.name.lower()
    return bool(blocked & lowered) or name.endswith(".spec.ts") or name.endswith(".test.ts")


def line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, max(0, offset)) + 1


def line_text(text: str, line: int) -> str:
    lines = text.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1].strip()
    return ""


def load_authoritative_ids(input_root: Path) -> set[str]:
    ids: set[str] = set()
    table_path = input_root / "tables.csv"
    if table_path.exists():
        with table_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                ids.add(table_id(row["database"], row["table_code"]))
                child = input_root / "tables" / f"{row['table_code']}.csv"
                if child.exists():
                    with child.open(encoding="utf-8", newline="") as column_handle:
                        ids.update(column_id(row["database"], row["table_code"], item["column_code"]) for item in csv.DictReader(column_handle))
    jobs_path = input_root / "jobnet.csv"
    if jobs_path.exists():
        with jobs_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                ids.add(stable_node_id("job-network", "batch-system", row["jobnet_id"]))
                ids.add(stable_node_id("job", "batch-system", row["jobnet_id"], row["job_id"]))
    return ids
