#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from contract.graph_contract import (
    column_id,
    normalize_oracle_identifier,
    normalize_repository_path,
    stable_node_id,
    table_id,
    validate_stable_id,
)
from extractors.package_support.package_writer import load_config

_VERSION = "1.0.0"
TABLE_HEADER = ["database", "table_code", "table_name_ja", "table_name_en"]
COLUMN_HEADER = [
    "column_code",
    "column_name_ja",
    "column_name_en",
    "ordinal_position",
    "data_type",
    "nullable",
    "note",
    "relation_table",
]
JOBNET_HEADER = [
    "jobnet_id",
    "jobnet_name",
    "job_id",
    "job_name",
    "executable_name",
    "arguments",
    "predecessor_job_id",
]
MAPPING_HEADER = [
    "job_system",
    "executable_name",
    "executable_scope",
    "canonical_executable_name",
    "alias",
]
LOCALIZED_HEADER = [
    "target_type",
    "target_id",
    "field_name",
    "locale",
    "value",
    "source_kind",
    "review_status",
    "author_name",
    "created_at",
    "updated_at",
]
SEMANTIC_HEADER = [
    "semantic_id",
    "domain",
    "locale",
    "label",
    "definition",
    "aliases",
    "status",
]
AUTHORITATIVE_FILES = {
    "tables": "tables.csv",
    "jobnet": "jobnet.csv",
    "executableMappings": "executable-mappings.csv",
    "localizedMetadata": "localized-metadata.csv",
    "semanticDictionary": "semantic-dictionary.csv",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize authoritative CSV inputs for the graph import pipeline."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    normalize(load_config(Path(args.config).expanduser()))
    return 0


def normalize(config: dict) -> None:
    if config.get("type") != "csv-normalizer":
        raise ValueError("Config type must be csv-normalizer")
    source_root = Path(config["root"]).resolve()
    output = Path(config["output"]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = config.get("files", {})

    table_summary = _normalize_tables(
        source_root,
        _source(source_root, files, "tables", AUTHORITATIVE_FILES["tables"]),
        output,
    )
    jobnet_summary = _normalize_jobnet(
        _source(source_root, files, "jobnet", AUTHORITATIVE_FILES["jobnet"]),
        output / AUTHORITATIVE_FILES["jobnet"],
    )
    known_targets = set(table_summary["stableIds"]) | set(jobnet_summary["stableIds"])
    summaries = [
        table_summary,
        jobnet_summary,
        _normalize_mappings(
            _source(
                source_root,
                files,
                "executableMappings",
                AUTHORITATIVE_FILES["executableMappings"],
            ),
            output / AUTHORITATIVE_FILES["executableMappings"],
        ),
        _normalize_localized(
            _source(
                source_root,
                files,
                "localizedMetadata",
                AUTHORITATIVE_FILES["localizedMetadata"],
            ),
            output / AUTHORITATIVE_FILES["localizedMetadata"],
            known_targets,
        ),
        _normalize_semantics(
            _source(
                source_root,
                files,
                "semanticDictionary",
                AUTHORITATIVE_FILES["semanticDictionary"],
            ),
            output / AUTHORITATIVE_FILES["semanticDictionary"],
        ),
    ]

    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    report = {
        "normalizer": "csv-normalizer",
        "version": _VERSION,
        "contractVersion": "1.0",
        "sourceId": config.get("source", "authoritative-csv"),
        "createdAt": generated_at,
        "files": {
            item["file"]: {
                "rows": item["rows"],
                "sha256": _sha256(output / item["file"]),
                "bytes": (
                    (output / item["file"]).stat().st_size
                    if (output / item["file"]).exists()
                    else 0
                ),
                "sourcePath": item["sourcePath"],
                "sourceSha256": item["sourceSha256"],
                "sourceBytes": item["sourceBytes"],
                "stableIdCount": len(item["stableIds"]),
            }
            for item in summaries
        },
        "stableIds": {item["file"]: item["stableIds"] for item in summaries},
        "totals": {
            "files": len(summaries),
            "presentFiles": sum(1 for item in summaries if item["present"]),
            "rows": sum(item["rows"] for item in summaries),
            "stableIds": sum(len(item["stableIds"]) for item in summaries),
            "issues": sum(len(item["issues"]) for item in summaries),
        },
        "issues": [issue for item in summaries for issue in item["issues"]],
    }
    (output / "normalization-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    _write_authoritative_manifest(output, config, summaries, generated_at)


def _source(root: Path, files: dict, key: str, default: str) -> Path:
    value = files.get(key, default) if isinstance(files, dict) else default
    path = Path(value)
    return path if path.is_absolute() else root / path


def _normalize_tables(source_root: Path, src: Path, output: Path) -> dict:
    issues = _missing_file_issues(src)
    rows = []
    stable_ids: set[str] = set()
    columns_output = output / "tables"
    columns_output.mkdir(exist_ok=True)
    if src.exists():
        for index, row in _read(src):
            database = _oracle(row.get("database"), issues, src, index, "database")
            table = _oracle(row.get("table_code"), issues, src, index, "table")
            if not database or not table:
                issues.append(
                    _issue(src, index, "INVALID_CONFIG", "Missing database/table")
                )
                continue
            normalized = {
                "database": database,
                "table_code": table,
                "table_name_ja": row.get("table_name_ja", ""),
                "table_name_en": row.get("table_name_en", ""),
                "_line": index,
            }
            rows.append(normalized)
            stable_ids.add(table_id(database, table))
            child_src = source_root / "tables" / f"{row.get('table_code', table)}.csv"
            child_rows = []
            if child_src.exists():
                for child_index, column in _read(child_src):
                    code = _oracle(
                        column.get("column_code"),
                        issues,
                        child_src,
                        child_index,
                        "column",
                    )
                    if not code:
                        continue
                    child_rows.append({**column, "column_code": code})
                    stable_ids.add(column_id(database, table, code))
            _write(columns_output / f"{table}.csv", COLUMN_HEADER, child_rows)
    rows = sorted(
        _dedupe(
            rows, ["database", "table_code"], issues, src, "Duplicate table row ignored"
        ),
        key=lambda r: (r["database"], r["table_code"]),
    )
    dst = output / AUTHORITATIVE_FILES["tables"]
    _write(dst, TABLE_HEADER, rows)
    return _summary(src, dst, rows, issues, stable_ids)


def _normalize_jobnet(src: Path, dst: Path) -> dict:
    issues = _missing_file_issues(src)
    rows = []
    if src.exists():
        for index, row in _read(src):
            job_system = _slug_part(row.get("job_system") or "batch-system")
            jobnet = _upper(row.get("jobnet_id") or row.get("jobnet"))
            job = _upper(row.get("job_id") or row.get("job"))
            executable = (
                row.get("executable_name") or row.get("executable") or ""
            ).strip()
            if not jobnet or not job or not executable:
                issues.append(
                    _issue(
                        src, index, "INVALID_CONFIG", "Missing jobnet/job/executable"
                    )
                )
                continue
            rows.append(
                {
                    "_job_system": job_system,
                    "jobnet_id": jobnet,
                    "jobnet_name": (row.get("jobnet_name") or jobnet).strip(),
                    "job_id": job,
                    "job_name": (row.get("job_name") or job).strip(),
                    "executable_name": executable,
                    "arguments": (row.get("arguments") or "").strip(),
                    "predecessor_job_id": _upper(row.get("predecessor_job_id")),
                    "_line": index,
                }
            )
    rows = sorted(
        _dedupe(
            rows,
            ["jobnet_id", "job_id"],
            issues,
            src,
            "Duplicate jobnet/job row ignored",
        ),
        key=lambda r: (r["jobnet_id"], r["job_id"]),
    )
    job_keys = {(row["jobnet_id"], row["job_id"]) for row in rows}
    for row in rows:
        predecessor = row.get("predecessor_job_id", "")
        if predecessor and (row["jobnet_id"], predecessor) not in job_keys:
            issues.append(
                _issue(
                    src,
                    int(row.get("_line") or 0),
                    "INVALID_CONFIG",
                    f"Missing predecessor job: {predecessor}",
                    severity="WARNING",
                    code="MISSING_PREDECESSOR",
                )
            )
    stable_ids = sorted(
        {
            stable_node_id(
                "job-network", row.get("_job_system", "batch-system"), row["jobnet_id"]
            )
            for row in rows
        }
        | {
            stable_node_id(
                "job",
                row.get("_job_system", "batch-system"),
                row["jobnet_id"],
                row["job_id"],
            )
            for row in rows
        }
    )
    _write(dst, JOBNET_HEADER, rows)
    return _summary(src, dst, rows, issues, stable_ids)


def _normalize_mappings(src: Path, dst: Path) -> dict:
    issues = _missing_file_issues(src)
    rows = []
    if src.exists():
        for index, row in _read(src):
            executable = (
                row.get("executable_name") or row.get("executable") or ""
            ).strip()
            canonical = _canonical_executable(
                row.get("canonical_executable_name") or executable
            )
            scope = _slug_part(
                row.get("executable_scope") or row.get("scope") or "batch-system"
            )
            if not executable:
                issues.append(
                    _issue(src, index, "INVALID_CONFIG", "Missing executable_name")
                )
                continue
            rows.append(
                {
                    "job_system": _slug_part(row.get("job_system") or "batch-system"),
                    "executable_name": executable,
                    "executable_scope": scope,
                    "canonical_executable_name": canonical,
                    "alias": _slug_part(row.get("alias") or ""),
                    "_line": index,
                }
            )
    rows = sorted(
        _dedupe(
            rows,
            ["job_system", "executable_name", "executable_scope"],
            issues,
            src,
            "Duplicate executable mapping row ignored",
        ),
        key=lambda r: (
            r["job_system"],
            r["executable_scope"],
            r["executable_name"].lower(),
        ),
    )
    stable_ids = sorted(
        {
            stable_node_id(
                "executable-mapping",
                row["job_system"],
                row["executable_scope"],
                row["canonical_executable_name"],
            )
            for row in rows
            if row["canonical_executable_name"]
        }
    )
    _write(dst, MAPPING_HEADER, rows)
    return _summary(src, dst, rows, issues, stable_ids)


def _normalize_localized(src: Path, dst: Path, known_targets: set[str]) -> dict:
    issues = _missing_file_issues(src)
    rows = []
    if src.exists():
        for index, row in _read(src):
            target_type = _upper(row.get("target_type") or row.get("type"))
            target_id = (row.get("target_id") or "").strip()
            locale = (row.get("locale") or "en").strip().lower()
            value = (row.get("value") or "").strip()
            if not target_type or not target_id or not value:
                issues.append(
                    _issue(
                        src,
                        index,
                        "INVALID_CONFIG",
                        "Missing localized metadata target/value",
                    )
                )
                continue
            if not _valid_stable_target(target_id, target_type, issues, src, index):
                continue
            if target_id not in known_targets:
                issues.append(
                    _issue(
                        src,
                        index,
                        "INVALID_CONFIG",
                        f"Localized target is not authoritative: {target_id}",
                        severity="WARNING",
                        code="UNKNOWN_LOCALIZED_TARGET",
                    )
                )
                continue
            rows.append(
                {
                    "target_type": target_type,
                    "target_id": target_id,
                    "field_name": (row.get("field_name") or "name").strip(),
                    "locale": locale,
                    "value": value,
                    "source_kind": _upper(row.get("source_kind") or "IMPORTED"),
                    "review_status": _upper(row.get("review_status") or "APPROVED"),
                    "author_name": (row.get("author_name") or "csv-normalizer").strip(),
                    "created_at": (row.get("created_at") or "").strip(),
                    "updated_at": (
                        row.get("updated_at") or row.get("created_at") or ""
                    ).strip(),
                    "_line": index,
                }
            )
    rows = sorted(
        _dedupe(
            rows,
            ["target_type", "target_id", "field_name", "locale"],
            issues,
            src,
            "Duplicate localized metadata row ignored",
        ),
        key=lambda r: (r["target_type"], r["target_id"], r["field_name"], r["locale"]),
    )
    stable_ids = sorted({row["target_id"] for row in rows})
    _write(dst, LOCALIZED_HEADER, rows)
    return _summary(src, dst, rows, issues, stable_ids)


def _normalize_semantics(src: Path, dst: Path) -> dict:
    issues = _missing_file_issues(src)
    rows = []
    if src.exists():
        for index, row in _read(src):
            semantic_id = (row.get("semantic_id") or "").strip().lower()
            status = (row.get("status") or "approved").strip().lower()
            if (
                not re.fullmatch(r"semantic:[a-z0-9][a-z0-9:_-]*", semantic_id)
                or not row.get("label")
                or status not in {"draft", "approved", "deprecated"}
            ):
                issues.append(
                    _issue(
                        src,
                        index,
                        "INVALID_CONFIG",
                        f"Invalid semantic dictionary row: {semantic_id or '<missing>'}",
                    )
                )
                continue
            rows.append(
                {
                    "semantic_id": semantic_id,
                    "domain": (row.get("domain") or "").strip(),
                    "locale": (row.get("locale") or "en").strip().lower(),
                    "label": (row.get("label") or "").strip(),
                    "definition": (row.get("definition") or "").strip(),
                    "aliases": "|".join(
                        dict.fromkeys(
                            value.strip()
                            for value in (row.get("aliases") or "").split("|")
                            if value.strip()
                        )
                    ),
                    "status": status,
                    "_line": index,
                }
            )
    rows = sorted(
        _dedupe(
            rows,
            ["semantic_id", "locale"],
            issues,
            src,
            "Duplicate semantic row ignored",
        ),
        key=lambda row: (row["semantic_id"], row["locale"]),
    )
    _write(dst, SEMANTIC_HEADER, rows)
    return _summary(src, dst, rows, issues, {row["semantic_id"] for row in rows})


def _read(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 2):
            yield index, {
                _field_name(key): (value or "").strip()
                for key, value in row.items()
                if key
            }


def _write(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        writer.writerows([{key: row.get(key, "") for key in header} for row in rows])


def _dedupe(
    rows: list[dict], keys: list[str], issues: list[dict], src: Path, message: str
) -> list[dict]:
    seen: OrderedDict[tuple, dict] = OrderedDict()
    for row in rows:
        key = tuple(row[key] for key in keys)
        if key in seen:
            issues.append(
                _issue(
                    src,
                    int(row.get("_line") or 0),
                    "INVALID_CONFIG",
                    message,
                    severity="WARNING",
                    code="DUPLICATE_ROW",
                    rowKey="|".join(str(item) for item in key),
                )
            )
            continue
        seen[key] = row
    return list(seen.values())


def _summary(
    src: Path,
    dst: Path,
    rows: list[dict],
    issues: list[dict],
    stable_ids: Iterable[str],
) -> dict:
    return {
        "file": dst.name,
        "rows": len(rows),
        "present": src.exists(),
        "sourcePath": _safe_report_path(src),
        "sourceSha256": _sha256(src),
        "sourceBytes": src.stat().st_size if src.exists() else 0,
        "stableIds": sorted(stable_ids),
        "issues": issues,
    }


def _missing_file_issues(src: Path) -> list[dict]:
    if src.exists():
        return []
    return [
        _issue(
            src,
            0,
            "INVALID_CONFIG",
            "Missing authoritative CSV file",
            severity="ERROR",
            code="MISSING_FILE",
        )
    ]


def _write_authoritative_manifest(
    output: Path, config: dict, summaries: list[dict], generated_at: str
) -> None:
    groups = {item["file"]: item for item in summaries}
    manifest = {
        "contractVersion": "1.0",
        "manifestKind": "authoritative-csv-normalization",
        "normalizer": {"name": "csv-normalizer", "version": _VERSION},
        "source": {
            "sourceKey": config.get("source", "authoritative-csv"),
            "repositoryKey": "manual-data",
        },
        "generatedAt": generated_at,
        "files": {
            "tables": AUTHORITATIVE_FILES["tables"],
            "jobnet": AUTHORITATIVE_FILES["jobnet"],
            "executableMappings": AUTHORITATIVE_FILES["executableMappings"],
            "localizedMetadata": AUTHORITATIVE_FILES["localizedMetadata"],
        },
        "statistics": {
            "filesScanned": sum(1 for item in summaries if item["present"]),
            "tables": groups[AUTHORITATIVE_FILES["tables"]]["rows"],
            "jobnet": groups[AUTHORITATIVE_FILES["jobnet"]]["rows"],
            "executableMappings": groups[AUTHORITATIVE_FILES["executableMappings"]][
                "rows"
            ],
            "localizedMetadata": groups[AUTHORITATIVE_FILES["localizedMetadata"]][
                "rows"
            ],
            "issues": sum(len(item["issues"]) for item in summaries),
        },
        "checksums": {
            item["file"]: {
                "sha256": _sha256(output / item["file"]),
                "bytes": (
                    (output / item["file"]).stat().st_size
                    if (output / item["file"]).exists()
                    else 0
                ),
            }
            for item in summaries
        },
        "metadata": {
            "importMode": "import_authoritative",
            "packageManifestFile": False,
            "report": "normalization-report.json",
        },
    }
    (output / "authoritative-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _field_name(value: object) -> str:
    return (
        str(value or "")
        .strip()
        .lstrip("\ufeff")
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _first(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key, "")
        if value:
            return value
    return ""


def _oracle(value: object, issues: list[dict], src: Path, line: int, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return normalize_oracle_identifier(text)
    except ValueError as exc:
        issues.append(_issue(src, line, "INVALID_CONFIG", f"Invalid {field}: {exc}"))
        return ""


def _upper(value: object) -> str:
    return str(value or "").strip().upper()


def _int(value: object, issues: list[dict], src: Path, line: int, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        issues.append(
            _issue(src, line, "INVALID_CONFIG", f"Invalid integer {field}: {value}")
        )
        return 0


def _bool(value: object, issues: list[dict], src: Path, line: int, field: str) -> str:
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y", "nullable", ""}:
        return "true"
    if text in {"false", "f", "0", "no", "n", "not null"}:
        return "false"
    issues.append(
        _issue(src, line, "INVALID_CONFIG", f"Invalid boolean {field}: {value}")
    )
    return "true"


def _canonical_executable(value: object) -> str:
    raw = str(value or "").strip().strip('"').strip("'").replace("\\", "/")
    return Path(raw).name.casefold()


def _slug_part(value: object) -> str:
    text = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-")


def _valid_stable_target(
    target_id: str, target_type: str, issues: list[dict], src: Path, line: int
) -> bool:
    try:
        validate_stable_id(
            target_id,
            (
                target_id.split(":", 1)[0].replace("-", "_").upper()
                if target_type == "NODE"
                else target_type
            ),
        )
        return True
    except ValueError as exc:
        issues.append(
            _issue(src, line, "INVALID_CONFIG", f"Invalid localized target id: {exc}")
        )
        return False


def _issue(
    path: Path,
    line: int,
    issue_type: str,
    message: str,
    severity: str = "ERROR",
    code: str = "",
    rowKey: str = "",
) -> dict:
    source_path = _safe_report_path(path)
    identity = f"{source_path}|{line}|{issue_type}|{severity}|{code}|{rowKey}|{message}"
    issue = {
        "issueId": "normalizer-issue:"
        + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "issueType": issue_type,
        "severity": severity,
        "sourcePath": source_path,
        "file": source_path,
        "line": line,
        "message": message,
    }
    if code:
        issue["code"] = code
    if rowKey:
        issue["rowKey"] = rowKey
    return issue


def _safe_report_path(path: Path) -> str:
    try:
        return normalize_repository_path(
            str(path.resolve().relative_to(WORKSPACE_ROOT.resolve()))
        )
    except (OSError, ValueError):
        fallback = path.name or "unknown"
        try:
            return normalize_repository_path(fallback)
        except ValueError:
            return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


if __name__ == "__main__":
    raise SystemExit(main())
