from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .comments import Comment
from .contract.graph_contract import canonical_edge_id, normalize_repository_path, stable_node_id

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
        try:
            manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            manifest = {}
        manifest_files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
        for name in CSV_HEADERS:
            configured = manifest_files.get(name, f"{name}.csv") if isinstance(manifest_files, dict) else f"{name}.csv"
            filenames = configured if isinstance(configured, list) else [configured]
            for filename in filenames:
                if not isinstance(filename, str):
                    continue
                path = package_dir / filename
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
            for node in self.nodes.values():
                try:
                    properties = json.loads(node.get("properties_json") or "{}")
                except json.JSONDecodeError:
                    continue
                if _replace_ref_node_id(properties, old_target, new_target):
                    node["properties_json"] = _json(properties)
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

    def write(
        self,
        output: Path,
        *,
        source_name: str,
        config_path: str,
        extractor_version: str = "1.0.0",
        max_csv_rows: int = 1_000_000,
        max_csv_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_csv_rows < 1:
            raise ValueError("max_csv_rows must be positive")
        if max_csv_bytes < 1024:
            raise ValueError("max_csv_bytes must be at least 1024")
        output.mkdir(parents=True, exist_ok=True)
        groups = {name: sorted(getattr(self, name).values(), key=lambda row: row[headers[0]]) for name, headers in CSV_HEADERS.items()}
        checksums: dict[str, dict[str, object]] = {}
        files: dict[str, str | list[str]] = {}
        for name, rows in groups.items():
            chunks = _csv_chunks(name, rows, CSV_HEADERS[name], max_csv_rows, max_csv_bytes)
            paths = [
                output / (f"{name}.csv" if len(chunks) == 1 else f"{name}-{index:06d}.csv")
                for index in range(1, len(chunks) + 1)
            ]
            for path, chunk in zip(paths, chunks):
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=CSV_HEADERS[name], lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(chunk)
                checksums[path.name] = {"sha256": _file_sha256(path), "bytes": path.stat().st_size, "rows": len(chunk)}
            names = [path.name for path in paths]
            files[name] = names[0] if len(names) == 1 else names
        manifest = {
            "contractVersion": "1.1",
            "extractor": {"name": "code-tree-exporter", "version": extractor_version},
            "source": {"sourceKey": source_name, "repositoryKey": source_name},
            "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "files": files,
            "statistics": {"filesScanned": len(self.source_records), **{name: len(rows) for name, rows in groups.items()}},
            "checksums": checksums,
            "metadata": {
                "managedBy": "code-tree-exporter",
                "config": Path(config_path).name,
                "sourceFiles": [
                    {key: value for key, value in record.__dict__.items() if key != "comments"}
                    for record in sorted(self.source_records, key=lambda item: (item.source_key, item.relative_path))
                ],
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _write_codebase_memory(self, output)
        _write_knowledge(self, output)

    def _add_edge(self, source: str, target: str, edge_type: str, layer: str) -> None:
        edge_id = canonical_edge_id(source, edge_type, target, "", layer)
        self.edges.setdefault(edge_id, {
            "edge_id": edge_id, "source_node_id": source, "target_node_id": target,
            "edge_type": edge_type, "graph_layer": layer, "raw_operation": "",
            "confidence": "1.0", "properties_json": "{}",
        })


def _write_codebase_memory(graph: GraphPackage, output: Path) -> None:
    memory_dir = output / "codebase-memory"
    entities_dir = memory_dir / "entities"
    relationships_dir = memory_dir / "relationships"
    summaries_dir = memory_dir / "summaries"
    for d in (entities_dir, relationships_dir, summaries_dir):
        d.mkdir(parents=True, exist_ok=True)

    entities_file = entities_dir / "nodes.jsonl"
    with entities_file.open("w", encoding="utf-8") as f:
        for node_id, node in graph.nodes.items():
            record = {
                "memory_id": f"mem:node:{node_id}",
                "node_id": node_id,
                "kind": node.get("node_type", ""),
                "source": node.get("repository_key", ""),
                "qualified_name": node.get("qualified_name", ""),
                "display_name": node.get("default_display_name", ""),
                "summary": "",
                "evidence_ids": [],
                "related_node_ids": [],
                "issue_ids": [],
                "knowledge_refs": [],
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "content_hash": hashlib.sha256(json.dumps(node, sort_keys=True).encode("utf-8")).hexdigest()[:16]
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    rels_file = relationships_dir / "edges.jsonl"
    with rels_file.open("w", encoding="utf-8") as f:
        for edge_id, edge in graph.edges.items():
            record = {
                "memory_id": f"mem:edge:{edge_id}",
                "edge_ids": [edge_id],
                "kind": edge.get("edge_type", ""),
                "from_node_id": edge.get("source_node_id", ""),
                "to_node_id": edge.get("target_node_id", ""),
                "path_node_ids": [],
                "summary": "",
                "confidence": edge.get("confidence", "1.0"),
                "evidence_ids": [],
                "issue_ids": []
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    manifest = {
        "version": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": {
            "entities": ["entities/nodes.jsonl"],
            "relationships": ["relationships/edges.jsonl"],
            "summaries": []
        }
    }
    (memory_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_knowledge(graph: GraphPackage, output: Path) -> None:
    knowledge_dir = output / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    apis_file = knowledge_dir / "APIs.md"
    with apis_file.open("w", encoding="utf-8") as f:
        f.write("# APIs\n\n")
        for node_id, node in graph.nodes.items():
            if node.get("node_type") == "API_OPERATION":
                f.write(f"## {node.get('default_display_name', '')}\n\n")
                f.write("```yaml\n")
                f.write(f"node_id: {node_id}\n")
                f.write(f"source: {node.get('repository_key', '')}\n")
                f.write(f"kind: {node.get('node_type', '')}\n")
                f.write("```\n\n")
                f.write(f"Qualified name: `{node.get('qualified_name', '')}`\n\n")

    manifest = {
        "version": "1.0",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "files": ["APIs.md"]
    }
    (knowledge_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def replace_directory(staging: Path, output: Path) -> None:
    backup = output.with_name(output.name + ".previous")
    _require_managed_directory(staging)
    _require_managed_directory(output)
    _require_managed_directory(backup)
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


def _json(value: dict[str, object] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_output_directory(output: Path) -> None:
    _require_managed_directory(output)
    _require_managed_directory(output.with_name(output.name + ".previous"))


def _require_managed_directory(path: Path) -> None:
    if not path.exists():
        return
    if not path.is_dir():
        raise ValueError(f"Output path is not a directory: {path}")
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Refusing to replace unmanaged output directory: {path}") from exc
    extractor = manifest.get("extractor") if isinstance(manifest, dict) else None
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
    managed = (
        isinstance(extractor, dict) and extractor.get("name") == "code-tree-exporter"
    ) or (
        isinstance(metadata, dict) and metadata.get("managedBy") == "code-tree-exporter"
    )
    if not managed:
        raise ValueError(f"Refusing to replace unmanaged output directory: {path}")


def _csv_chunks(
    name: str,
    rows: list[dict[str, str]],
    headers: list[str],
    max_rows: int,
    max_bytes: int,
) -> list[list[dict[str, str]]]:
    header_bytes = len(_csv_text(headers, None).encode("utf-8"))
    chunks: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    current_bytes = header_bytes
    for row in rows:
        row_bytes = len(_csv_text(headers, row).encode("utf-8"))
        if header_bytes + row_bytes > max_bytes:
            raise ValueError(f"{name} row exceeds maxCsvBytes={max_bytes}: {row.get(headers[0], '')}")
        if current and (len(current) >= max_rows or current_bytes + row_bytes > max_bytes):
            chunks.append(current)
            current = []
            current_bytes = header_bytes
        current.append(row)
        current_bytes += row_bytes
    chunks.append(current)
    return chunks


def _csv_text(headers: list[str], row: dict[str, str] | None) -> str:
    buffer = io.StringIO(newline="")
    if row is None:
        csv.writer(buffer, lineterminator="\n").writerow(headers)
    else:
        csv.DictWriter(buffer, fieldnames=headers, lineterminator="\n").writerow(row)
    return buffer.getvalue()


def _replace_ref_node_id(value: object, old_target: str, new_target: str) -> bool:
    changed = False
    if isinstance(value, dict):
        if value.get("ref_node_id") == old_target:
            value["ref_node_id"] = new_target
            value["resolution"] = "resolved"
            changed = True
        ref_node_ids = value.get("ref_node_ids")
        if isinstance(ref_node_ids, list) and old_target in ref_node_ids:
            value["ref_node_ids"] = [new_target if item == old_target else item for item in ref_node_ids]
            changed = True
        for child in value.values():
            changed = _replace_ref_node_id(child, old_target, new_target) or changed
    elif isinstance(value, list):
        for child in value:
            changed = _replace_ref_node_id(child, old_target, new_target) or changed
    return changed


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
