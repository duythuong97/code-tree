from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import re
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from code_tree_exporter.contract.graph_contract import (
    column_id,
    database_id,
    normalize_oracle_identifier,
    stable_node_id,
    table_id,
)
from code_tree_exporter.decoding import SourceDecodingError, decode_source
from code_tree_exporter.extractors.package_support.package_writer import PackageBuilder

if TYPE_CHECKING:
    from code_tree_exporter.graph_package import GraphPackage


_INITIAL_FILE = re.compile(
    r"^database-(?P<kind>tables|columns)__(?P<database>[A-Za-z0-9_.-]+)\.csv$",
    re.IGNORECASE,
)
_TABLE_FIELDS = (
    "database_key",
    "schema_name",
    "object_name",
    "object_type",
    "status",
    "comment",
)
_COLUMN_FIELDS = (
    "database_key",
    "schema_name",
    "object_name",
    "column_name",
    "ordinal_position",
    "data_type",
    "data_length",
    "data_precision",
    "data_scale",
    "nullable",
    "default_value",
    "primary_key",
    "comment",
)


@dataclass(frozen=True)
class CatalogTable:
    database_key: str
    schema_name: str
    object_name: str
    object_type: str
    status: str
    comment: str
    source_path: str
    source_line: int

    @property
    def key(self) -> tuple[str, str, str]:
        return self.database_key, self.schema_name, self.object_name


@dataclass(frozen=True)
class CatalogColumn:
    database_key: str
    schema_name: str
    object_name: str
    column_name: str
    ordinal_position: str
    data_type: str
    data_length: str
    data_precision: str
    data_scale: str
    nullable: str
    default_value: str
    primary_key: str
    comment: str
    source_path: str
    source_line: int

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.database_key,
            self.schema_name,
            self.object_name,
            self.column_name,
        )


@dataclass(frozen=True)
class CatalogFileReport:
    path: str
    catalog_type: str
    database_key: str
    profile_name: str
    sha256: str
    bytes: int
    rows_read: int
    rows_imported: int
    rows_rejected: int
    status: str
    message: str = ""


@dataclass(frozen=True)
class CatalogProfile:
    name: str
    catalog_type: str
    filename: str
    required_headers: tuple[str, ...]
    fields: dict[str, object]
    transforms: dict[str, tuple[str, ...]]
    output_file: str = ""
    output_fields: tuple[str, ...] = ()
    identity_fields: tuple[str, ...] = ()

    def matches(self, path: Path, headers: tuple[str, ...]) -> bool:
        if self.filename and not fnmatch.fnmatch(path.name, self.filename):
            return False
        available = {header.casefold() for header in headers}
        return all(header.casefold() in available for header in self.required_headers)


@dataclass
class NormalizedCsv:
    fields: tuple[str, ...]
    identity_fields: tuple[str, ...]
    rows: dict[tuple[str, ...], dict[str, str]] = field(default_factory=dict)


@dataclass
class CatalogImportResult:
    normalized_root: Path
    tables: dict[tuple[str, str, str], CatalogTable] = field(default_factory=dict)
    columns: dict[tuple[str, str, str, str], CatalogColumn] = field(default_factory=dict)
    normalized_csvs: dict[str, NormalizedCsv] = field(default_factory=dict)
    files: list[CatalogFileReport] = field(default_factory=list)
    issues: list[dict[str, object]] = field(default_factory=list)

    def merge_into(self, graph: GraphPackage, *, system_key: str) -> None:
        builder = PackageBuilder(
            "catalog-v3",
            "catalog:v3",
            "catalog-importer",
            "3.0.0",
            {"capabilities": ["database-catalog", "catalog-provenance"]},
        )
        system_id = stable_node_id("system", system_key)
        builder.add_node(
            system_id,
            "SYSTEM",
            system_key,
            system_key,
            system_key,
            system_key=system_key,
            repository_key="catalog",
            properties={"catalog_managed": True, "extractor_contract": "3.0"},
        )

        leaf_counts: dict[tuple[str, str], int] = {}
        for table in self.tables.values():
            key = table.database_key, table.object_name
            leaf_counts[key] = leaf_counts.get(key, 0) + 1

        table_ids: dict[tuple[str, str, str], str] = {}
        for table in sorted(self.tables.values(), key=lambda item: item.key):
            database_node_id = database_id(table.database_key)
            schema_node_id = stable_node_id(
                "database-schema", table.database_key, table.schema_name
            )
            table_token = (
                table.object_name
                if leaf_counts[(table.database_key, table.object_name)] == 1
                else f"{table.schema_name}.{table.object_name}"
            )
            object_node_id = table_id(table.database_key, table_token)
            table_ids[table.key] = object_node_id
            builder.add_node(
                database_node_id,
                "DATABASE",
                table.database_key,
                table.database_key,
                table.database_key,
                system_key=system_key,
                database_key=table.database_key,
                repository_key="catalog",
                properties={"catalog_managed": True},
            )
            builder.add_node(
                schema_node_id,
                "DATABASE_SCHEMA",
                table.schema_name,
                f"{table.database_key}.{table.schema_name}",
                table.schema_name,
                system_key=system_key,
                database_key=table.database_key,
                repository_key="catalog",
                properties={
                    "database": table.database_key,
                    "schema": table.schema_name,
                    "catalog_managed": True,
                },
            )
            builder.add_node(
                object_node_id,
                _catalog_node_type(table.object_type),
                table.object_name,
                f"{table.database_key}.{table.schema_name}.{table.object_name}",
                table.object_name,
                system_key=system_key,
                database_key=table.database_key,
                repository_key="catalog",
                properties={
                    "database": table.database_key,
                    "schema": table.schema_name,
                    "table": table.object_name,
                    "object_name": table.object_name,
                    "object_type": table.object_type,
                    "status": table.status,
                    "comment": table.comment,
                    "catalog_managed": True,
                    "provenance": "EXTRACTED",
                },
            )
            builder.add_edge(system_id, database_node_id, "CONTAINS", graph_layer="STRUCTURAL")
            builder.add_edge(database_node_id, schema_node_id, "CONTAINS", graph_layer="STRUCTURAL")
            builder.add_edge(schema_node_id, object_node_id, "CONTAINS", graph_layer="STRUCTURAL")
            builder.add_evidence(
                "NODE",
                object_node_id,
                table.source_path,
                table.source_line,
                table.source_line,
                "CATALOG_ROW",
                ",".join(
                    (
                        table.database_key,
                        table.schema_name,
                        table.object_name,
                        table.object_type,
                    )
                ),
                properties={"catalog_type": "database-tables"},
            )

        for column in sorted(self.columns.values(), key=lambda item: item.key):
            owner_id = table_ids.get(
                (column.database_key, column.schema_name, column.object_name)
            )
            if not owner_id:
                continue
            table_token = owner_id.split(":", 2)[-1]
            column_node_id = column_id(
                column.database_key, table_token, column.column_name
            )
            builder.add_node(
                column_node_id,
                "COLUMN",
                column.column_name,
                (
                    f"{column.database_key}.{column.schema_name}."
                    f"{column.object_name}.{column.column_name}"
                ),
                column.column_name,
                system_key=system_key,
                database_key=column.database_key,
                repository_key="catalog",
                graph_role="TECHNICAL",
                properties={
                    "database": column.database_key,
                    "schema": column.schema_name,
                    "table": column.object_name,
                    "column": column.column_name,
                    "ordinal_position": column.ordinal_position,
                    "data_type": column.data_type,
                    "data_length": column.data_length,
                    "data_precision": column.data_precision,
                    "data_scale": column.data_scale,
                    "nullable": column.nullable,
                    "default_value": column.default_value,
                    "primary_key": column.primary_key,
                    "comment": column.comment,
                    "catalog_managed": True,
                    "provenance": "EXTRACTED",
                },
            )
            builder.add_edge(owner_id, column_node_id, "CONTAINS", graph_layer="STRUCTURAL")
            builder.add_evidence(
                "NODE",
                column_node_id,
                column.source_path,
                column.source_line,
                column.source_line,
                "CATALOG_ROW",
                ",".join(
                    (
                        column.database_key,
                        column.schema_name,
                        column.object_name,
                        column.column_name,
                        column.data_type,
                    )
                ),
                properties={"catalog_type": "database-columns"},
            )

        _merge_builder(graph, builder)
        for issue in self.issues:
            graph.add_issue(
                str(issue["issue_type"]),
                str(issue["message"]),
                source_path=str(issue.get("source_path") or ""),
                severity=str(issue.get("severity") or "WARNING"),
                properties={
                    key: value
                    for key, value in issue.items()
                    if key not in {"issue_type", "message", "source_path", "severity"}
                },
            )


def prepare_catalog(
    config: dict[str, object],
    *,
    config_dir: Path,
    staging_root: Path,
    existing_input: Path | None = None,
) -> CatalogImportResult | None:
    configured = config.get("catalog")
    if configured is None:
        return None
    if isinstance(configured, str):
        options: dict[str, object] = {"folder": configured}
    elif isinstance(configured, dict):
        options = configured
    else:
        raise ValueError("catalog must be a path string or an object")
    if options.get("autoImport", True) is False:
        return None
    raw_folder = options.get("folder")
    if not isinstance(raw_folder, str) or not raw_folder.strip():
        raise ValueError("catalog.folder must be a non-empty path")
    catalog_root = Path(raw_folder).expanduser()
    if not catalog_root.is_absolute():
        catalog_root = config_dir / catalog_root
    catalog_root = catalog_root.resolve()
    if not catalog_root.is_dir():
        raise ValueError(f"catalog.folder must be an existing directory: {catalog_root}")
    incoming = catalog_root / "incoming"
    scan_root = incoming if incoming.is_dir() else catalog_root
    normalized_root = staging_root / "catalog-normalized"
    if existing_input and existing_input.is_dir():
        shutil.copytree(existing_input, normalized_root, dirs_exist_ok=True)
    else:
        normalized_root.mkdir(parents=True, exist_ok=True)

    result = CatalogImportResult(normalized_root)
    profiles = _load_profiles(catalog_root / "profiles")
    duplicate_policy = str(options.get("duplicatePolicy") or "error").lower()
    if duplicate_policy not in {"error", "first-wins", "last-wins"}:
        raise ValueError(
            "catalog.duplicatePolicy must be error, first-wins, or last-wins"
        )
    encoding = str(options.get("encoding") or "auto")
    csv_paths = sorted(
        path
        for path in scan_root.rglob("*.csv")
        if path.is_file() and "profiles" not in {part.lower() for part in path.parts}
    )
    for path in csv_paths:
        _import_file(
            result,
            path,
            scan_root=scan_root,
            profiles=profiles,
            duplicate_policy=duplicate_policy,
            encoding=encoding,
        )
    _reject_orphan_columns(result)
    _write_legacy_catalog(result)
    if options.get("strict") is True and any(
        report.status == "rejected" or report.rows_rejected
        for report in result.files
    ):
        raise ValueError("catalog import failed in strict mode; inspect catalog issues")
    return result


def inspect_catalog_file(path: Path, *, encoding: str = "auto") -> dict[str, object]:
    resolved = path.expanduser().resolve()
    text = _decode_csv(resolved, resolved.name, encoding)
    reader = csv.reader(io.StringIO(text))
    headers = next(reader, [])
    match = _INITIAL_FILE.match(resolved.name)
    return {
        "path": str(resolved),
        "filename": resolved.name,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
        "headers": [header.strip() for header in headers],
        "detectedType": f"database-{match.group('kind').lower()}" if match else None,
        "databaseKey": match.group("database").upper() if match else None,
        "profileTemplate": {
            "name": resolved.stem,
            "catalogType": "",
            "match": {
                "filename": resolved.name,
                "requiredHeaders": [header.strip() for header in headers],
            },
            "fields": {},
            "transforms": {},
        },
    }


def _import_file(
    result: CatalogImportResult,
    path: Path,
    *,
    scan_root: Path,
    profiles: tuple[CatalogProfile, ...],
    duplicate_policy: str,
    encoding: str,
) -> None:
    relative = path.relative_to(scan_root).as_posix()
    source_path = f"catalog/{relative}"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = _decode_csv(path, source_path, encoding)
        reader = csv.DictReader(io.StringIO(text))
        headers = tuple((header or "").strip() for header in (reader.fieldnames or ()))
        profile, filename_database = _select_profile(path, headers, profiles)
        if profile is None:
            result.files.append(
                CatalogFileReport(
                    source_path,
                    "unknown",
                    "",
                    "",
                    digest,
                    len(raw),
                    0,
                    0,
                    0,
                    "rejected",
                    f"No catalog profile matched headers: {list(headers)}",
                )
            )
            result.issues.append(
                {
                    "issue_type": "CATALOG_UNKNOWN_SCHEMA",
                    "severity": "WARNING",
                    "message": "No catalog profile matched CSV structure",
                    "source_path": source_path,
                    "headers": list(headers),
                }
            )
            return
        rows_read = rows_imported = rows_rejected = 0
        for row_number, raw_row in enumerate(reader, 2):
            rows_read += 1
            try:
                mapped = _map_row(profile, raw_row, filename_database)
                if profile.catalog_type == "database-tables":
                    record = _table_record(mapped, source_path, row_number, filename_database)
                    inserted = _put_record(
                        result.tables, record.key, record, duplicate_policy
                    )
                elif profile.catalog_type == "database-columns":
                    record = _column_record(mapped, source_path, row_number, filename_database)
                    inserted = _put_record(
                        result.columns, record.key, record, duplicate_policy
                    )
                elif profile.output_file:
                    inserted = _put_normalized_row(
                        result, profile, mapped, duplicate_policy
                    )
                else:
                    raise ValueError(
                        f"Catalog profile {profile.name} must define output.filename"
                    )
                if inserted:
                    rows_imported += 1
                else:
                    rows_rejected += 1
                    _catalog_issue(
                        result,
                        "CATALOG_CONFLICT",
                        "Duplicate catalog identity was not imported",
                        source_path,
                        row_number,
                    )
            except (KeyError, ValueError) as exc:
                rows_rejected += 1
                _catalog_issue(
                    result,
                    "CATALOG_INVALID_ROW",
                    str(exc),
                    source_path,
                    row_number,
                )
        result.files.append(
            CatalogFileReport(
                source_path,
                profile.catalog_type,
                filename_database,
                profile.name,
                digest,
                len(raw),
                rows_read,
                rows_imported,
                rows_rejected,
                "imported" if not rows_rejected else "partial",
            )
        )
    except (OSError, SourceDecodingError, csv.Error, ValueError) as exc:
        result.files.append(
            CatalogFileReport(
                source_path,
                "unknown",
                "",
                "",
                digest,
                len(raw),
                0,
                0,
                0,
                "rejected",
                str(exc),
            )
        )
        _catalog_issue(
            result,
            "CATALOG_INVALID_FILE",
            str(exc),
            source_path,
            None,
        )


def _select_profile(
    path: Path,
    headers: tuple[str, ...],
    profiles: tuple[CatalogProfile, ...],
) -> tuple[CatalogProfile | None, str]:
    match = _INITIAL_FILE.match(path.name)
    if match:
        kind = match.group("kind").lower()
        profile = CatalogProfile(
            f"builtin-database-{kind}",
            f"database-{kind}",
            path.name,
            ("database_key", "schema_name", "object_name")
            + (("column_name",) if kind == "columns" else ()),
            {field: field for field in (_COLUMN_FIELDS if kind == "columns" else _TABLE_FIELDS)},
            {},
        )
        if profile.matches(path, headers):
            return profile, normalize_oracle_identifier(match.group("database"))
    matches = [profile for profile in profiles if profile.matches(path, headers)]
    if len(matches) > 1:
        raise ValueError(
            f"Multiple catalog profiles match {path.name}: "
            + ", ".join(profile.name for profile in matches)
        )
    if matches:
        database = ""
        match = _INITIAL_FILE.match(path.name)
        if match:
            database = normalize_oracle_identifier(match.group("database"))
        return matches[0], database
    return None, ""


def _load_profiles(path: Path) -> tuple[CatalogProfile, ...]:
    if not path.is_dir():
        return ()
    result: list[CatalogProfile] = []
    for profile_path in sorted(path.glob("*.json")):
        value = json.loads(profile_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"Catalog profile must be a JSON object: {profile_path}")
        match = value.get("match") if isinstance(value.get("match"), dict) else {}
        fields = value.get("fields") if isinstance(value.get("fields"), dict) else {}
        transforms_value = (
            value.get("transforms") if isinstance(value.get("transforms"), dict) else {}
        )
        required_headers = tuple(str(item) for item in match.get("requiredHeaders", ()))
        if not required_headers:
            raise ValueError(
                f"Catalog profile match.requiredHeaders cannot be empty: {profile_path}"
            )
        if not fields:
            raise ValueError(f"Catalog profile fields cannot be empty: {profile_path}")
        output = value.get("output") if isinstance(value.get("output"), dict) else {}
        output_file = _safe_output_file(str(output.get("filename") or ""))
        output_fields = tuple(str(item) for item in output.get("fields", ()))
        identity_fields = tuple(str(item) for item in output.get("identity", ()))
        catalog_type = str(value.get("catalogType") or "")
        if not catalog_type:
            raise ValueError(f"Catalog profile catalogType is required: {profile_path}")
        if len(set(output_fields)) != len(output_fields) or any(
            not item for item in output_fields
        ):
            raise ValueError(
                f"Catalog profile output.fields must be unique non-empty names: {profile_path}"
            )
        if catalog_type not in {"database-tables", "database-columns"}:
            if not output_file or not output_fields:
                raise ValueError(
                    f"Catalog profile requires output.filename and output.fields: {profile_path}"
                )
            if not set(identity_fields).issubset(output_fields):
                raise ValueError(
                    f"Catalog profile output.identity must use output.fields: {profile_path}"
                )
        result.append(
            CatalogProfile(
                str(value.get("name") or profile_path.stem),
                catalog_type,
                str(match.get("filename") or "*.csv"),
                required_headers,
                {str(key): item for key, item in fields.items()},
                {
                    str(key): tuple(str(item) for item in values)
                    for key, values in transforms_value.items()
                    if isinstance(values, list)
                },
                output_file,
                output_fields,
                identity_fields,
            )
        )
    return tuple(result)


def _map_row(
    profile: CatalogProfile,
    raw_row: dict[str | None, str | None],
    filename_database: str,
) -> dict[str, str]:
    row = {
        str(key).strip().casefold(): str(value or "").strip()
        for key, value in raw_row.items()
        if key is not None
    }
    mapped: dict[str, str] = {}
    for target, specification in profile.fields.items():
        if isinstance(specification, str):
            value = row.get(specification.casefold(), "")
        elif isinstance(specification, dict):
            if "constant" in specification:
                value = str(specification.get("constant") or "")
            elif specification.get("fromFilename") == "database_key":
                value = filename_database
            else:
                source = str(specification.get("column") or "")
                value = row.get(source.casefold(), "")
                if not value:
                    value = str(specification.get("default") or "")
        else:
            value = ""
        for transform in profile.transforms.get(target, ()):
            value = _transform(value, transform)
        mapped[target] = value.strip()
    mapped.setdefault("database_key", filename_database)
    return mapped


def _transform(value: str, transform: str) -> str:
    if transform == "trim":
        return value.strip()
    if transform == "upper":
        return value.upper()
    if transform == "lower":
        return value.lower()
    if transform == "bool":
        return "Y" if value.strip().casefold() in {"1", "true", "yes", "y"} else "N"
    raise ValueError(f"Unsupported catalog transform: {transform}")


def _table_record(
    row: dict[str, str], source_path: str, line: int, filename_database: str
) -> CatalogTable:
    database = _required_identifier(row, "database_key")
    if filename_database and database != filename_database:
        raise ValueError(
            f"database_key {database} does not match filename database {filename_database}"
        )
    object_type = str(row.get("object_type") or "TABLE").strip().upper()
    if object_type not in {"TABLE", "VIEW", "MATERIALIZED_VIEW"}:
        raise ValueError(f"Unsupported database object_type: {object_type}")
    return CatalogTable(
        database,
        _required_identifier(row, "schema_name"),
        _required_identifier(row, "object_name"),
        object_type,
        str(row.get("status") or "").strip().upper(),
        str(row.get("comment") or "").strip(),
        source_path,
        line,
    )


def _column_record(
    row: dict[str, str], source_path: str, line: int, filename_database: str
) -> CatalogColumn:
    database = _required_identifier(row, "database_key")
    if filename_database and database != filename_database:
        raise ValueError(
            f"database_key {database} does not match filename database {filename_database}"
        )
    return CatalogColumn(
        database,
        _required_identifier(row, "schema_name"),
        _required_identifier(row, "object_name"),
        _required_identifier(row, "column_name"),
        str(row.get("ordinal_position") or "").strip(),
        str(row.get("data_type") or "").strip().upper(),
        str(row.get("data_length") or "").strip(),
        str(row.get("data_precision") or "").strip(),
        str(row.get("data_scale") or "").strip(),
        str(row.get("nullable") or "").strip().upper(),
        str(row.get("default_value") or "").strip(),
        str(row.get("primary_key") or "").strip().upper(),
        str(row.get("comment") or "").strip(),
        source_path,
        line,
    )


def _required_identifier(row: dict[str, str], field_name: str) -> str:
    value = str(row.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"Missing required catalog field: {field_name}")
    return normalize_oracle_identifier(value)


def _put_record(
    collection: dict[tuple[str, ...], object],
    key: tuple[str, ...],
    value: object,
    duplicate_policy: str,
) -> bool:
    current = collection.get(key)
    if current is None:
        collection[key] = value
        return True
    if current == value:
        return True
    if duplicate_policy == "last-wins":
        collection[key] = value
        return True
    return duplicate_policy == "first-wins"


def _put_normalized_row(
    result: CatalogImportResult,
    profile: CatalogProfile,
    mapped: dict[str, str],
    duplicate_policy: str,
) -> bool:
    fields = profile.output_fields
    missing = [name for name in fields if name not in mapped]
    if missing:
        raise ValueError(f"Profile does not map output fields: {missing}")
    identity_fields = profile.identity_fields or fields
    dataset = result.normalized_csvs.get(profile.output_file)
    if dataset is None:
        dataset = NormalizedCsv(fields, identity_fields)
        result.normalized_csvs[profile.output_file] = dataset
    elif dataset.fields != fields or dataset.identity_fields != identity_fields:
        raise ValueError(
            f"Profiles targeting {profile.output_file} must use identical output fields and identity"
        )
    row = {name: mapped[name] for name in fields}
    key = tuple(row[name] for name in identity_fields)
    current = dataset.rows.get(key)
    if current is None or current == row:
        dataset.rows[key] = row
        return True
    if duplicate_policy == "last-wins":
        dataset.rows[key] = row
        return True
    return duplicate_policy == "first-wins"


def _reject_orphan_columns(result: CatalogImportResult) -> None:
    retained: dict[tuple[str, str, str, str], CatalogColumn] = {}
    rejected_by_file: dict[str, int] = {}
    for key, column in result.columns.items():
        table_key = key[:3]
        if table_key in result.tables:
            retained[key] = column
            continue
        _catalog_issue(
            result,
            "CATALOG_REFERENCE_MISSING",
            "Column references a table that is absent from database-tables catalog",
            column.source_path,
            column.source_line,
        )
        rejected_by_file[column.source_path] = (
            rejected_by_file.get(column.source_path, 0) + 1
        )
    result.columns = retained
    if rejected_by_file:
        result.files = [
            replace(
                report,
                rows_imported=max(
                    0, report.rows_imported - rejected_by_file.get(report.path, 0)
                ),
                rows_rejected=(
                    report.rows_rejected + rejected_by_file.get(report.path, 0)
                ),
                status=(
                    "partial" if rejected_by_file.get(report.path, 0) else report.status
                ),
            )
            for report in result.files
        ]


def _write_legacy_catalog(result: CatalogImportResult) -> None:
    tables_path = result.normalized_root / "tables.csv"
    tables_path.parent.mkdir(parents=True, exist_ok=True)
    with tables_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("database", "table_code", "schema", "columns_file"),
        )
        writer.writeheader()
        for table in sorted(result.tables.values(), key=lambda item: item.key):
            relative = _column_file_path(table.key)
            writer.writerow(
                {
                    "database": table.database_key,
                    "table_code": table.object_name,
                    "schema": table.schema_name,
                    "columns_file": relative.as_posix(),
                }
            )
            columns = [
                column
                for column in result.columns.values()
                if column.key[:3] == table.key
            ]
            target = result.normalized_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", encoding="utf-8", newline="") as column_handle:
                column_writer = csv.DictWriter(
                    column_handle,
                    fieldnames=(
                        "column_code",
                        "data_type",
                        "nullable",
                        "ordinal_position",
                        "schema",
                    ),
                )
                column_writer.writeheader()
                for column in sorted(columns, key=lambda item: item.key):
                    column_writer.writerow(
                        {
                            "column_code": column.column_name,
                            "data_type": column.data_type,
                            "nullable": column.nullable,
                            "ordinal_position": column.ordinal_position,
                            "schema": column.schema_name,
                        }
                    )
    for relative, dataset in sorted(result.normalized_csvs.items()):
        target = result.normalized_root.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=dataset.fields)
            writer.writeheader()
            for key in sorted(dataset.rows):
                writer.writerow(dataset.rows[key])


def _column_file_path(key: tuple[str, str, str]) -> Path:
    digest = hashlib.sha256("|".join(key).encode("utf-8")).hexdigest()[:24]
    return Path("tables") / "v3" / f"{digest}.csv"


def _safe_output_file(value: str) -> str:
    if not value:
        return ""
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or ".." in path.parts
        or path.suffix.casefold() != ".csv"
        or normalized.casefold() == "tables.csv"
        or normalized.casefold().startswith("tables/v3/")
    ):
        raise ValueError(f"Unsafe catalog profile output filename: {value}")
    return path.as_posix()


def _decode_csv(path: Path, source_path: str, encoding: str) -> str:
    return decode_source(path, source_path, encoding).text


def _catalog_node_type(object_type: str) -> str:
    return {
        "TABLE": "TABLE",
        "VIEW": "VIEW",
        "MATERIALIZED_VIEW": "MATERIALIZED_VIEW",
    }[object_type]


def _catalog_issue(
    result: CatalogImportResult,
    issue_type: str,
    message: str,
    source_path: str,
    source_line: int | None,
) -> None:
    result.issues.append(
        {
            "issue_type": issue_type,
            "severity": "WARNING",
            "message": message,
            "source_path": source_path,
            "source_line": source_line,
        }
    )


def _merge_builder(graph: GraphPackage, builder: PackageBuilder) -> None:
    for name in ("nodes", "edges", "evidence", "issues"):
        source = getattr(builder, name)
        target = getattr(graph, name)
        for key, row in source.items():
            current = target.get(key)
            if current is not None and current != row:
                graph.conflicts.append((name, key))
                continue
            target[key] = row
