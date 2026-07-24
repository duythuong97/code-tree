from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from comments import Comment
from contract.graph_contract import canonical_edge_id, normalize_repository_path, stable_node_id

CSV_HEADERS = {
    "nodes": "node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json".split(","),
    "edges": "edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json".split(","),
    "evidence": "evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json".split(","),
    "comments": "comment_id,source_path,owner_node_id,comment_kind,start_line,end_line,start_column,end_column,raw_text,normalized_text,language,encoding,properties_json".split(","),
    "issues": "issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json".split(","),
}


@dataclass(frozen=True)
class SourceRecord:
    source_key: str
    source_type: str
    system_key: str
    repository_key: str
    relative_path: str
    declared_encoding: str
    actual_encoding: str
    raw_sha256: str
    text_sha256: str
    newline_style: str
    bom: str
    comments: tuple[Comment, ...] = ()


@dataclass
class GraphPackage:
    nodes: dict[str, dict[str, str]] = field(default_factory=dict)
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    evidence: dict[str, dict[str, str]] = field(default_factory=dict)
    comments: dict[str, dict[str, str]] = field(default_factory=dict)
    issues: dict[str, dict[str, str]] = field(default_factory=dict)
    source_records: list[SourceRecord] = field(default_factory=list)
    conflicts: list[tuple[str, str]] = field(default_factory=list)

    def merge_directory(self, package_dir: Path) -> None:
        for name in ("nodes", "edges", "evidence", "issues"):
            path = package_dir / f"{name}.csv"
            if not path.exists():
                continue
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    key = row[CSV_HEADERS[name][0]]
                    current = getattr(self, name).get(key)
                    if current is not None and current != row:
                        self.conflicts.append((name, key))
                        continue
                    getattr(self, name)[key] = row

    def resolve_routine_references(self) -> None:
        routines: dict[tuple[str, str, str], list[str]] = {}
        for node_id, node in self.nodes.items():
            if node.get("node_type") not in {"PROCEDURE", "FUNCTION"}:
                continue
            parts = node_id.split(":")
            if len(parts) < 4:
                continue
            key = (node.get("database_key", "").upper(), parts[2].upper(), node.get("technical_name", "").upper())
            routines.setdefault(key, []).append(node_id)
        replacements: dict[str, str] = {}
        for node_id, node in self.nodes.items():
            if node.get("node_type") != "UNRESOLVED_REFERENCE":
                continue
            try:
                properties = json.loads(node.get("properties_json") or "{}")
            except json.JSONDecodeError:
                continue
            key = tuple(str(properties.get(name, "")).upper() for name in ("database", "package", "routine"))
            candidates = routines.get(key, [])
            if len(candidates) == 1:
                replacements[node_id] = candidates[0]
        for old_target, new_target in replacements.items():
            edge_ids: dict[str, str] = {}
            for old_edge_id, edge in list(self.edges.items()):
                if edge["target_node_id"] != old_target:
                    continue
                edge["target_node_id"] = new_target
                new_edge_id = canonical_edge_id(edge["source_node_id"], edge["edge_type"], new_target, edge["raw_operation"], edge["graph_layer"])
                edge["edge_id"] = new_edge_id
                edge_ids[old_edge_id] = new_edge_id
                del self.edges[old_edge_id]
                self.edges[new_edge_id] = edge
            for evidence in self.evidence.values():
                if evidence.get("target_type") == "EDGE" and evidence.get("target_id") in edge_ids:
                    evidence["target_id"] = edge_ids[evidence["target_id"]]
            if not any(edge["target_node_id"] == old_target for edge in self.edges.values()):
                del self.nodes[old_target]

    def add_source(self, record: SourceRecord) -> None:
        canonical_path = normalize_repository_path(record.relative_path)
        record = SourceRecord(**{**record.__dict__, "relative_path": canonical_path})
        self.source_records.append(record)
        file_id = stable_node_id("file", record.source_key, record.relative_path)
        properties = {
            "source_type": record.source_type,
            "declared_encoding": record.declared_encoding,
            "actual_encoding": record.actual_encoding,
            "raw_sha256": record.raw_sha256,
            "text_sha256": record.text_sha256,
            "newline_style": record.newline_style,
            "bom": record.bom,
        }
        self.nodes[file_id] = {
            "node_id": file_id, "node_type": "FILE", "technical_name": PurePosixPath(record.relative_path).name,
            "qualified_name": f"{record.source_key}/{record.relative_path}", "default_display_name": record.relative_path,
            "system_key": record.system_key, "database_key": "", "repository_key": record.repository_key,
            "graph_role": "EVIDENCE", "confidence": "1.0", "properties_json": _json(properties),
        }
        for comment in record.comments:
            self.comments[comment.comment_id] = {
                "comment_id": comment.comment_id, "source_path": comment.source_path,
                "owner_node_id": file_id, "comment_kind": comment.comment_kind,
                "start_line": str(comment.start_line), "end_line": str(comment.end_line),
                "start_column": str(comment.start_column), "end_column": str(comment.end_column),
                "raw_text": comment.raw_text, "normalized_text": comment.normalized_text,
                "language": comment.language, "encoding": record.actual_encoding,
                "properties_json": _json({"classification": comment.classification}),
            }

    def add_issue(self, issue_type: str, message: str, *, source_path: str = "", properties: dict[str, object] | None = None, severity: str = "ERROR") -> None:
        identity = f"{issue_type}|{source_path}|{message}|{_json(properties)}"
        issue_id = "issue:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.issues[issue_id] = {
            "issue_id": issue_id, "issue_type": issue_type, "severity": severity,
            "source_node_id": "", "raw_reference": "", "database_key": "",
            "source_path": source_path, "start_line": "", "message": message,
            "properties_json": _json(properties),
        }

    def materialize_structure(self) -> None:
        for record in self.source_records:
            file_id = stable_node_id("file", record.source_key, record.relative_path)
            system_id = stable_node_id("system", record.system_key or record.source_key)
            self.nodes.setdefault(system_id, {
                "node_id": system_id, "node_type": "SYSTEM", "technical_name": record.system_key or record.source_key,
                "qualified_name": record.system_key or record.source_key, "default_display_name": record.system_key or record.source_key,
                "system_key": record.system_key, "database_key": "", "repository_key": record.repository_key,
                "graph_role": "MAIN", "confidence": "1.0", "properties_json": "{}",
            })
            self._add_edge(system_id, file_id, "CONTAINS", "STRUCTURAL")
        declarations_by_path: dict[str, list[tuple[str, int, int]]] = {}
        for row in self.evidence.values():
            if row.get("target_type") != "NODE" or row.get("target_id") not in self.nodes:
                continue
            start = int(row["start_line"]) if row.get("start_line", "").isdigit() else 0
            end = int(row["end_line"]) if row.get("end_line", "").isdigit() else start
            declarations_by_path.setdefault(row.get("source_path", ""), []).append((row["target_id"], start, end))
        structural_children = {
            edge["target_node_id"]
            for edge in self.edges.values()
            if edge.get("edge_type") == "CONTAINS" and edge.get("graph_layer") == "STRUCTURAL"
        }
        for record in self.source_records:
            file_id = stable_node_id("file", record.source_key, record.relative_path)
            declarations = [
                item for item in declarations_by_path.get(record.relative_path, [])
                if self.nodes[item[0]].get("repository_key") in {"", record.repository_key}
            ]
            for node_id, _, _ in sorted(declarations, key=lambda item: (item[1], item[2], item[0])):
                if node_id != file_id and node_id not in structural_children:
                    self._add_edge(file_id, node_id, "CONTAINS", "STRUCTURAL")
            for comment in self.comments.values():
                if comment["source_path"] != record.relative_path:
                    continue
                start_line = int(comment["start_line"])
                end_line = int(comment["end_line"])
                containing = [item for item in declarations if item[1] <= start_line and end_line <= item[2]]
                if containing:
                    owner = min(containing, key=lambda item: (item[2] - item[1], -item[1]))[0]
                else:
                    owner = next((node_id for node_id, line, _ in sorted(declarations, key=lambda item: item[1]) if end_line <= line <= end_line + 2), file_id)
                comment["owner_node_id"] = owner
        for name, key in self.conflicts:
            self.add_issue("MERGE_CONFLICT", f"Conflicting {name} row: {key}", severity="WARNING")

    def write(self, output: Path, *, source_name: str, config_path: str, extractor_version: str = "1.0.0") -> None:
        output.mkdir(parents=True, exist_ok=True)
        groups = {name: sorted(getattr(self, name).values(), key=lambda row: row[headers[0]]) for name, headers in CSV_HEADERS.items()}
        checksums: dict[str, dict[str, object]] = {}
        for name, rows in groups.items():
            path = output / f"{name}.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS[name], lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            checksums[path.name] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}
        manifest = {
            "contractVersion": "1.0",
            "extractor": {"name": "code-tree-exporter", "version": extractor_version},
            "source": {"sourceKey": source_name, "repositoryKey": source_name},
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": {name: f"{name}.csv" for name in groups},
            "statistics": {"filesScanned": len(self.source_records), **{name: len(rows) for name, rows in groups.items()}},
            "checksums": checksums,
            "metadata": {
                "config": Path(config_path).name,
                "sourceFiles": [
                    {key: value for key, value in record.__dict__.items() if key != "comments"}
                    for record in sorted(self.source_records, key=lambda item: (item.source_key, item.relative_path))
                ],
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _add_edge(self, source: str, target: str, edge_type: str, layer: str) -> None:
        edge_id = canonical_edge_id(source, edge_type, target, "", layer)
        self.edges.setdefault(edge_id, {
            "edge_id": edge_id, "source_node_id": source, "target_node_id": target,
            "edge_type": edge_type, "graph_layer": layer, "raw_operation": "",
            "confidence": "1.0", "properties_json": "{}",
        })


def replace_directory(staging: Path, output: Path) -> None:
    backup = output.with_name(output.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if output.exists():
        output.replace(backup)
    try:
        staging.replace(output)
    except Exception:
        if backup.exists() and not output.exists():
            backup.replace(output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _json(value: dict[str, object] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
