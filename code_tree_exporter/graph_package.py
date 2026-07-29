from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

csv.field_size_limit(sys.maxsize)

from .comments import Comment
from .contract.graph_contract import (
    canonical_edge_id,
    canonical_evidence_key,
    normalize_repository_path,
    stable_node_id,
)

CSV_HEADERS = {
    "nodes": "node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json".split(
        ","
    ),
    "edges": "edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json".split(
        ","
    ),
    "evidence": "evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json".split(
        ","
    ),
    "comments": "comment_id,source_path,owner_node_id,comment_kind,start_line,end_line,start_column,end_column,raw_text,normalized_text,language,encoding,properties_json".split(
        ","
    ),
    "issues": "issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json".split(
        ","
    ),
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
    source_descriptors: dict[str, dict[str, str]] = field(default_factory=dict)
    conflicts: list[tuple[str, str]] = field(default_factory=list)
    _identifiers_compacted: bool = False

    def deduplicate(self) -> None:
        """Collapse rows that describe the same semantic graph entity."""
        if self._identifiers_compacted:
            return

        node_aliases: dict[str, str] = {}
        nodes: dict[str, dict[str, str]] = {}
        node_keys: dict[tuple[str, ...], str] = {}
        for old_id in sorted(self.nodes, key=lambda value: (len(value), value)):
            row = dict(self.nodes[old_id])
            key = _node_dedupe_key(old_id, row)
            retained_id = node_keys.get(key) if key is not None else None
            if retained_id is None:
                retained_id = old_id
                if key is not None:
                    node_keys[key] = retained_id
                row["node_id"] = retained_id
                nodes[retained_id] = row
            else:
                _merge_duplicate_row(nodes[retained_id], row)
            node_aliases[old_id] = retained_id

        for row in nodes.values():
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), node_aliases
            )
        self.nodes = nodes

        edge_aliases: dict[str, str] = {}
        edges: dict[str, dict[str, str]] = {}
        for old_id in sorted(self.edges, key=lambda value: (len(value), value)):
            row = dict(self.edges[old_id])
            row["source_node_id"] = node_aliases.get(
                row.get("source_node_id", ""), row.get("source_node_id", "")
            )
            row["target_node_id"] = node_aliases.get(
                row.get("target_node_id", ""), row.get("target_node_id", "")
            )
            retained_id = canonical_edge_id(
                row["source_node_id"],
                row.get("edge_type", ""),
                row["target_node_id"],
                row.get("raw_operation", ""),
                row.get("graph_layer", ""),
            )
            row["edge_id"] = retained_id
            if retained_id in edges:
                _merge_duplicate_row(edges[retained_id], row)
            else:
                edges[retained_id] = row
            edge_aliases[old_id] = retained_id
        self.edges = edges

        evidence_aliases: dict[str, str] = {}
        evidence: dict[str, dict[str, str]] = {}
        evidence_keys: dict[tuple[str, ...], str] = {}
        for old_id in sorted(self.evidence, key=lambda value: (len(value), value)):
            row = dict(self.evidence[old_id])
            target_aliases = (
                node_aliases if row.get("target_type") == "NODE" else edge_aliases
            )
            target_id = row.get("target_id", "")
            row["target_id"] = target_aliases.get(target_id, target_id)
            key = canonical_evidence_key(row)
            retained_id = evidence_keys.get(key)
            if retained_id is None:
                retained_id = old_id
                evidence_keys[key] = retained_id
                row["evidence_id"] = retained_id
                evidence[retained_id] = row
            else:
                _merge_duplicate_row(evidence[retained_id], row)
            evidence_aliases[old_id] = retained_id
        self.evidence = evidence

        comment_aliases: dict[str, str] = {}
        comments: dict[str, dict[str, str]] = {}
        comment_keys: dict[tuple[str, ...], str] = {}
        for old_id in sorted(self.comments, key=lambda value: (len(value), value)):
            row = dict(self.comments[old_id])
            owner_id = row.get("owner_node_id", "")
            row["owner_node_id"] = node_aliases.get(owner_id, owner_id)
            key = _comment_dedupe_key(row)
            retained_id = comment_keys.get(key)
            if retained_id is None:
                retained_id = old_id
                comment_keys[key] = retained_id
                row["comment_id"] = retained_id
                comments[retained_id] = row
            else:
                _merge_duplicate_row(comments[retained_id], row)
            comment_aliases[old_id] = retained_id
        self.comments = comments

        issue_aliases: dict[str, str] = {}
        issues: dict[str, dict[str, str]] = {}
        issue_keys: dict[tuple[str, ...], str] = {}
        for old_id in sorted(self.issues, key=lambda value: (len(value), value)):
            row = dict(self.issues[old_id])
            source_id = row.get("source_node_id", "")
            row["source_node_id"] = node_aliases.get(source_id, source_id)
            key = _issue_dedupe_key(row)
            retained_id = issue_keys.get(key)
            if retained_id is None:
                retained_id = old_id
                issue_keys[key] = retained_id
                row["issue_id"] = retained_id
                issues[retained_id] = row
            else:
                _merge_duplicate_row(issues[retained_id], row)
            issue_aliases[old_id] = retained_id
        self.issues = issues

        aliases = {
            **node_aliases,
            **edge_aliases,
            **evidence_aliases,
            **comment_aliases,
            **issue_aliases,
        }
        for collection in (
            self.nodes,
            self.edges,
            self.evidence,
            self.comments,
            self.issues,
        ):
            for row in collection.values():
                row["properties_json"] = _rewrite_properties(
                    row.get("properties_json", "{}"), aliases
                )

    def compact_identifiers(self) -> None:
        if self._identifiers_compacted:
            return
        self.deduplicate()
        node_ids = _compact_id_map("node", self.nodes)
        edge_ids = _compact_id_map("edge", self.edges)
        evidence_ids = _compact_id_map("evidence", self.evidence)
        comment_ids = _compact_id_map("comment", self.comments)
        issue_ids = _compact_id_map("issue", self.issues)
        references = {
            **node_ids,
            **edge_ids,
            **evidence_ids,
            **comment_ids,
            **issue_ids,
        }

        compact_nodes: dict[str, dict[str, str]] = {}
        for stable_id, original in self.nodes.items():
            row = dict(original)
            row["node_id"] = node_ids[stable_id]
            row["stable_id"] = stable_id
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), references
            )
            compact_nodes[row["node_id"]] = row

        compact_edges: dict[str, dict[str, str]] = {}
        for stable_id, original in self.edges.items():
            row = dict(original)
            row["edge_id"] = edge_ids[stable_id]
            row["stable_id"] = stable_id
            row["source_node_id"] = _required_reference(
                node_ids, row.get("source_node_id", ""), "edge source"
            )
            row["target_node_id"] = _required_reference(
                node_ids, row.get("target_node_id", ""), "edge target"
            )
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), references
            )
            compact_edges[row["edge_id"]] = row

        compact_evidence: dict[str, dict[str, str]] = {}
        for stable_id, original in self.evidence.items():
            row = dict(original)
            row["evidence_id"] = evidence_ids[stable_id]
            row["stable_id"] = stable_id
            target_map = node_ids if row.get("target_type") == "NODE" else edge_ids
            row["target_id"] = _required_reference(
                target_map, row.get("target_id", ""), "evidence target"
            )
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), references
            )
            compact_evidence[row["evidence_id"]] = row

        compact_comments: dict[str, dict[str, str]] = {}
        for stable_id, original in self.comments.items():
            row = dict(original)
            row["comment_id"] = comment_ids[stable_id]
            row["stable_id"] = stable_id
            row["owner_node_id"] = _required_reference(
                node_ids, row.get("owner_node_id", ""), "comment owner"
            )
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), references
            )
            compact_comments[row["comment_id"]] = row

        compact_issues: dict[str, dict[str, str]] = {}
        for stable_id, original in self.issues.items():
            row = dict(original)
            row["issue_id"] = issue_ids[stable_id]
            row["stable_id"] = stable_id
            source_node_id = row.get("source_node_id", "")
            row["source_node_id"] = (
                _required_reference(node_ids, source_node_id, "issue source")
                if source_node_id
                else ""
            )
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), references
            )
            compact_issues[row["issue_id"]] = row

        self.nodes = compact_nodes
        self.edges = compact_edges
        self.evidence = compact_evidence
        self.comments = compact_comments
        self.issues = compact_issues
        self._identifiers_compacted = True

    def merge_directory(self, package_dir: Path) -> None:
        try:
            manifest = json.loads(
                (package_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            manifest = {}
        manifest_files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
        for name in CSV_HEADERS:
            configured = (
                manifest_files.get(name, f"{name}.csv")
                if isinstance(manifest_files, dict)
                else f"{name}.csv"
            )
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
        from .linker import link_routines

        link_routines(self)

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
            "node_id": file_id,
            "node_type": "FILE",
            "technical_name": PurePosixPath(record.relative_path).name,
            "qualified_name": f"{record.source_key}/{record.relative_path}",
            "default_display_name": record.relative_path,
            "system_key": record.system_key,
            "database_key": "",
            "repository_key": record.repository_key,
            "graph_role": "EVIDENCE",
            "confidence": "1.0",
            "properties_json": _json(properties),
        }
        for comment in record.comments:
            self.comments[comment.comment_id] = {
                "comment_id": comment.comment_id,
                "source_path": comment.source_path,
                "owner_node_id": file_id,
                "comment_kind": comment.comment_kind,
                "start_line": str(comment.start_line),
                "end_line": str(comment.end_line),
                "start_column": str(comment.start_column),
                "end_column": str(comment.end_column),
                "raw_text": comment.raw_text,
                "normalized_text": comment.normalized_text,
                "language": comment.language,
                "encoding": record.actual_encoding,
                "properties_json": _json({"classification": comment.classification}),
            }

    def register_source(
        self,
        source_key: str,
        source_type: str,
        system_key: str,
        repository_key: str,
    ) -> None:
        self.source_descriptors[source_key] = {
            "source_key": source_key,
            "source_type": source_type,
            "system_key": system_key,
            "repository_key": repository_key,
        }

    def add_issue(
        self,
        issue_type: str,
        message: str,
        *,
        source_path: str = "",
        properties: dict[str, object] | None = None,
        severity: str = "ERROR",
    ) -> None:
        identity = f"{issue_type}|{source_path}|{message}|{_json(properties)}"
        issue_id = "issue:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self.issues[issue_id] = {
            "issue_id": issue_id,
            "issue_type": issue_type,
            "severity": severity,
            "source_node_id": "",
            "raw_reference": "",
            "database_key": "",
            "source_path": source_path,
            "start_line": "",
            "message": message,
            "properties_json": _json(properties),
        }

    def materialize_structure(self) -> None:
        for record in self.source_records:
            file_id = stable_node_id("file", record.source_key, record.relative_path)
            system_id = stable_node_id("system", record.system_key or record.source_key)
            self.nodes.setdefault(
                system_id,
                {
                    "node_id": system_id,
                    "node_type": "SYSTEM",
                    "technical_name": record.system_key or record.source_key,
                    "qualified_name": record.system_key or record.source_key,
                    "default_display_name": record.system_key or record.source_key,
                    "system_key": record.system_key,
                    "database_key": "",
                    "repository_key": record.repository_key,
                    "graph_role": "MAIN",
                    "confidence": "1.0",
                    "properties_json": "{}",
                },
            )
            self._add_edge(system_id, file_id, "CONTAINS", "STRUCTURAL")
        declarations_by_path: dict[str, list[tuple[str, int, int]]] = {}
        for row in self.evidence.values():
            if (
                row.get("target_type") != "NODE"
                or row.get("target_id") not in self.nodes
            ):
                continue
            start = int(row["start_line"]) if row.get("start_line", "").isdigit() else 0
            end = int(row["end_line"]) if row.get("end_line", "").isdigit() else start
            declarations_by_path.setdefault(row.get("source_path", ""), []).append(
                (row["target_id"], start, end)
            )
        structural_children = {
            edge["target_node_id"]
            for edge in self.edges.values()
            if edge.get("edge_type") == "CONTAINS"
            and edge.get("graph_layer") == "STRUCTURAL"
        }
        for record in self.source_records:
            file_id = stable_node_id("file", record.source_key, record.relative_path)
            declarations = [
                item
                for item in declarations_by_path.get(record.relative_path, [])
                if self.nodes[item[0]].get("repository_key")
                in {"", record.repository_key}
            ]
            for node_id, _, _ in sorted(
                declarations, key=lambda item: (item[1], item[2], item[0])
            ):
                if node_id != file_id and node_id not in structural_children:
                    self._add_edge(file_id, node_id, "CONTAINS", "STRUCTURAL")
            for comment in self.comments.values():
                if comment["source_path"] != record.relative_path:
                    continue
                start_line = int(comment["start_line"])
                end_line = int(comment["end_line"])
                containing = [
                    item
                    for item in declarations
                    if item[1] <= start_line and end_line <= item[2]
                ]
                if containing:
                    owner = min(
                        containing, key=lambda item: (item[2] - item[1], -item[1])
                    )[0]
                else:
                    owner = next(
                        (
                            node_id
                            for node_id, line, _ in sorted(
                                declarations, key=lambda item: item[1]
                            )
                            if end_line <= line <= end_line + 2
                        ),
                        file_id,
                    )
                comment["owner_node_id"] = owner
        for name, key in self.conflicts:
            self.add_issue(
                "MERGE_CONFLICT", f"Conflicting {name} row: {key}", severity="WARNING"
            )

    def write_sqlite(
        self,
        output: Path,
        *,
        source_name: str,
        config_path: str,
        extractor_version: str = "0.5.0",
        output_mode: str = "flat",
        combined_projection: bool | None = None,
        knowledge_chunking: dict[str, object] | None = None,
        max_tree_lines: int = 20_000,
        max_evidence_snippet_chars: int = 500,
        max_issues_per_type_per_file: int = 20,
    ) -> None:
        if output_mode not in {"flat", "partitioned"}:
            raise ValueError("output_mode must be 'flat' or 'partitioned'")
        if max_evidence_snippet_chars < 1:
            raise ValueError("max_evidence_snippet_chars must be positive")
        if max_issues_per_type_per_file < 1:
            raise ValueError("max_issues_per_type_per_file must be positive")
        if max_tree_lines < 10:
            raise ValueError("max_tree_lines must be at least 10")
        output.mkdir(parents=True, exist_ok=True)
        generated_at = _utc_now()
        markdown_options = {
            "combinedProjection": (
                output_mode == "flat"
                if combined_projection is None
                else combined_projection
            ),
            "knowledgeChunking": knowledge_chunking or {},
            "maxTreeLines": max_tree_lines,
        }
        self.deduplicate()

        # Apply output bounds before compacting so synthesized issue rows also
        # receive deterministic integer identifiers.
        rows = _graph_rows(
            self,
            max_evidence_snippet_chars=max_evidence_snippet_chars,
            max_issues_per_type_per_file=max_issues_per_type_per_file,
        )
        self.evidence = {
            row["evidence_id"]: row for row in rows["evidence"]
        }
        self.issues = {row["issue_id"]: row for row in rows["issues"]}
        self.compact_identifiers()
        rows = _graph_rows(
            self,
            max_evidence_snippet_chars=max_evidence_snippet_chars,
            max_issues_per_type_per_file=max_issues_per_type_per_file,
        )
        partition = _partition_graph(self, rows)
        packages = _sqlite_package_map(self, source_name)
        node_packages = partition["node_packages"]
        edge_packages = partition["edge_packages"]
        evidence_packages = partition["evidence_packages"]
        issue_packages = partition["issue_packages"]
        database_path = output / "graph.sqlite"
        _write_sqlite_database(
            database_path,
            rows,
            graph=self,
            generated_at=generated_at,
            source_name=source_name,
            node_packages=node_packages,
            edge_packages=edge_packages,
            evidence_packages=evidence_packages,
            comment_packages=partition["comment_packages"],
            issue_packages=issue_packages,
            output_mode=output_mode,
            markdown_options=markdown_options,
        )
        root_manifest = {
            "contractVersion": "2.0",
            "storage": "sqlite",
            "extractor": {
                "name": "code-tree-exporter",
                "version": extractor_version,
            },
            "source": {
                "sourceKey": source_name,
                "repositoryKey": source_name,
            },
            "generatedAt": generated_at,
            "outputMode": output_mode,
            "packages": packages,
            "files": {
                "database": "graph.sqlite",
                "graphIndex": "graph-index.json",
                "memoryManifest": "codebase-memory/manifest.json",
            },
            "statistics": {
                "filesScanned": len(self.source_records),
                **{name: len(value) for name, value in rows.items()},
            },
            "checksums": {
                "graph.sqlite": {
                    "sha256": _file_sha256(database_path),
                    "bytes": database_path.stat().st_size,
                }
            },
            "metadata": {
                "managedBy": "code-tree-exporter",
                "config": Path(config_path).name,
                "sourceCount": len([key for key in packages if key != "global"]),
                "identity": "deterministic-63-bit",
                "artifactType": "extracted-graph",
            },
        }
        memory_refs = write_codebase_memory(
            self,
            output,
            generated_at=generated_at,
            knowledge_refs={},
            write_summaries=False,
        )
        _write_graph_index(
            self,
            output,
            generated_at=generated_at,
            packages=packages,
            node_packages=node_packages,
            edge_packages=edge_packages,
            evidence_packages=evidence_packages,
            issue_packages=issue_packages,
            knowledge_refs={},
            memory_refs=memory_refs,
        )
        (output / "manifest.json").write_text(
            json.dumps(root_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write(self, output: Path, **kwargs) -> None:
        """Compatibility wrapper for callers using the former publisher API."""
        self.write_sqlite(output, **kwargs)

    def _add_edge(self, source: str, target: str, edge_type: str, layer: str) -> None:
        edge_id = canonical_edge_id(source, edge_type, target, "", layer)
        self.edges.setdefault(
            edge_id,
            {
                "edge_id": edge_id,
                "source_node_id": source,
                "target_node_id": target,
                "edge_type": edge_type,
                "graph_layer": layer,
                "raw_operation": "",
                "confidence": "1.0",
                "properties_json": "{}",
            },
        )


def _node_dedupe_key(
    stable_id: str, row: dict[str, str]
) -> tuple[str, ...] | None:
    qualified_name = row.get("qualified_name", "").strip()
    if not qualified_name:
        return None
    node_type = row.get("node_type", "").strip().upper()
    discriminator = ""
    if node_type == "LOCAL_ROUTINE":
        discriminator = _routine_signature(stable_id, row)
    elif node_type in {"INLINE_SQL", "SQL_STATEMENT"}:
        discriminator = _sql_identity(row)
    return (
        node_type,
        row.get("system_key", "").strip(),
        row.get("database_key", "").strip(),
        row.get("repository_key", "").strip(),
        qualified_name,
        discriminator,
    )


def _routine_signature(stable_id: str, row: dict[str, str]) -> str:
    properties = _json_object(row.get("properties_json", "{}"))
    for key in ("signature", "parameter_signature"):
        value = properties.get(key)
        if isinstance(value, str) and value:
            return value
    semantic_tree = properties.get("semantic_tree")
    if isinstance(semantic_tree, dict):
        value = semantic_tree.get("signature")
        if isinstance(value, str) and value:
            return value
    return stable_id.rsplit(":", 1)[-1]


def _sql_identity(row: dict[str, str]) -> str:
    properties = _json_object(row.get("properties_json", "{}"))
    for key in ("normalized_sql", "sql", "statement"):
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return ""


def _comment_dedupe_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        row.get(name, "")
        for name in (
            "source_path",
            "owner_node_id",
            "comment_kind",
            "start_line",
            "end_line",
            "start_column",
            "end_column",
            "raw_text",
            "normalized_text",
            "language",
        )
    )


def _issue_dedupe_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        row.get(name, "")
        for name in (
            "issue_type",
            "source_node_id",
            "raw_reference",
            "database_key",
            "source_path",
            "start_line",
            "message",
        )
    )


def _merge_duplicate_row(target: dict[str, str], candidate: dict[str, str]) -> None:
    target["confidence"] = _maximum_number_text(
        target.get("confidence", ""), candidate.get("confidence", "")
    )
    target["graph_role"] = _preferred_value(
        target.get("graph_role", ""),
        candidate.get("graph_role", ""),
        ("MAIN", "TECHNICAL", "EVIDENCE"),
    )
    target["severity"] = _preferred_value(
        target.get("severity", ""),
        candidate.get("severity", ""),
        ("ERROR", "WARNING", "INFO"),
    )
    target["properties_json"] = _merge_properties_json(
        target.get("properties_json", "{}"), candidate.get("properties_json", "{}")
    )
    if len(candidate.get("snippet", "")) > len(target.get("snippet", "")):
        target["snippet"] = candidate["snippet"]
    for key, value in candidate.items():
        if key not in target or target[key] == "":
            target[key] = value


def _maximum_number_text(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    try:
        return left if float(left) >= float(right) else right
    except ValueError:
        return left


def _preferred_value(left: str, right: str, order: tuple[str, ...]) -> str:
    if not left:
        return right
    if not right:
        return left
    ranks = {value: index for index, value in enumerate(order)}
    return (
        left
        if ranks.get(left, len(order)) <= ranks.get(right, len(order))
        else right
    )


def _merge_properties_json(left: str, right: str) -> str:
    left_value = _json_object(left)
    right_value = _json_object(right)
    if not left_value:
        return _json(right_value) if right_value else left or right or "{}"
    if not right_value:
        return _json(left_value)
    return _json(_merge_json_values(left_value, right_value))


def _json_object(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _merge_json_values(left: object, right: object) -> object:
    if isinstance(left, dict) and isinstance(right, dict):
        result = dict(left)
        for key, value in right.items():
            result[key] = (
                _merge_json_values(result[key], value) if key in result else value
            )
        return result
    if isinstance(left, list) and isinstance(right, list):
        result = list(left)
        for value in right:
            if value not in result:
                result.append(value)
        return result
    return right if left in (None, "", [], {}) else left


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _compact_id_map(kind: str, rows: dict[str, dict[str, str]]) -> dict[str, str]:
    occupied: dict[int, str] = {}
    result: dict[str, str] = {}
    for stable_id in sorted(rows):
        salt = 0
        while True:
            payload = f"{kind}|{stable_id}|{salt}".encode("utf-8")
            value = int.from_bytes(
                hashlib.blake2b(payload, digest_size=8).digest(), "big"
            ) & ((1 << 63) - 1)
            value = value or 1
            existing = occupied.get(value)
            if existing is None or existing == stable_id:
                occupied[value] = stable_id
                result[stable_id] = str(value)
                break
            salt += 1
    return result


def _required_reference(
    identifiers: dict[str, str], stable_id: str, label: str
) -> str:
    try:
        return identifiers[stable_id]
    except KeyError as exc:
        raise ValueError(f"Unresolved {label} identifier: {stable_id}") from exc


def _rewrite_properties(value: str, identifiers: dict[str, str]) -> str:
    try:
        properties = json.loads(value or "{}")
    except json.JSONDecodeError:
        return value

    def rewrite(item):
        if isinstance(item, str):
            return identifiers.get(item, item)
        if isinstance(item, list):
            return [rewrite(value) for value in item]
        if isinstance(item, dict):
            return {key: rewrite(value) for key, value in item.items()}
        return item

    return _json(rewrite(properties))


def _sqlite_package_map(
    graph: GraphPackage, source_name: str
) -> dict[str, dict[str, object]]:
    source_types: dict[str, str] = {
        key: descriptor.get("source_type", "")
        for key, descriptor in graph.source_descriptors.items()
    }
    for record in graph.source_records:
        source_types.setdefault(record.source_key, record.source_type)
    packages = {
        key: {
            "database": "graph.sqlite",
            "scope": f"sources/{key}",
            "sourceType": source_type,
        }
        for key, source_type in sorted(source_types.items())
    }
    packages["global"] = {
        "database": "graph.sqlite",
        "scope": "global",
        "sourceType": "global",
    }
    if not source_types:
        packages[source_name] = {
            "database": "graph.sqlite",
            "scope": "global",
            "sourceType": "mixed",
        }
    return packages


def _write_sqlite_database(
    path: Path,
    rows: dict[str, list[dict[str, str]]],
    *,
    graph: GraphPackage,
    generated_at: str,
    source_name: str,
    node_packages: dict[str, str],
    edge_packages: dict[str, str],
    evidence_packages: dict[str, str],
    comment_packages: dict[str, str],
    issue_packages: dict[str, str],
    output_mode: str,
    markdown_options: dict[str, object],
) -> None:
    if path.exists():
        path.unlink()
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            CREATE TABLE sources (
                source_key TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                source_type TEXT NOT NULL,
                system_key TEXT NOT NULL,
                repository_key TEXT NOT NULL,
                declared_encoding TEXT NOT NULL,
                actual_encoding TEXT NOT NULL,
                raw_sha256 TEXT NOT NULL,
                text_sha256 TEXT NOT NULL,
                newline_style TEXT NOT NULL,
                bom TEXT NOT NULL,
                PRIMARY KEY (source_key, relative_path)
            ) WITHOUT ROWID;
            CREATE TABLE nodes (
                node_id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                node_type TEXT NOT NULL,
                technical_name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                default_display_name TEXT NOT NULL,
                system_key TEXT NOT NULL,
                database_key TEXT NOT NULL,
                repository_key TEXT NOT NULL,
                graph_role TEXT NOT NULL,
                confidence REAL NOT NULL,
                properties_json TEXT NOT NULL,
                package_key TEXT NOT NULL
            );
            CREATE TABLE edges (
                edge_id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                source_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
                target_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
                edge_type TEXT NOT NULL,
                graph_layer TEXT NOT NULL,
                raw_operation TEXT NOT NULL,
                confidence REAL NOT NULL,
                properties_json TEXT NOT NULL,
                package_key TEXT NOT NULL
            );
            CREATE TABLE evidence (
                evidence_id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                target_type TEXT NOT NULL CHECK (target_type IN ('NODE', 'EDGE')),
                target_id INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                start_column INTEGER,
                end_column INTEGER,
                evidence_kind TEXT NOT NULL,
                extractor_name TEXT NOT NULL,
                confidence REAL NOT NULL,
                snippet TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                package_key TEXT NOT NULL
            );
            CREATE TABLE comments (
                comment_id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                source_path TEXT NOT NULL,
                owner_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
                comment_kind TEXT NOT NULL,
                start_line INTEGER,
                end_line INTEGER,
                start_column INTEGER,
                end_column INTEGER,
                raw_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                language TEXT NOT NULL,
                encoding TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                package_key TEXT NOT NULL
            );
            CREATE TABLE issues (
                issue_id INTEGER PRIMARY KEY,
                stable_id TEXT NOT NULL UNIQUE,
                issue_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source_node_id INTEGER REFERENCES nodes(node_id),
                raw_reference TEXT NOT NULL,
                database_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                start_line INTEGER,
                message TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                package_key TEXT NOT NULL
            );
            CREATE INDEX idx_nodes_type ON nodes(node_type);
            CREATE INDEX idx_nodes_qualified_name ON nodes(qualified_name COLLATE NOCASE);
            CREATE INDEX idx_nodes_package ON nodes(package_key);
            CREATE INDEX idx_edges_source ON edges(source_node_id);
            CREATE INDEX idx_edges_target ON edges(target_node_id);
            CREATE INDEX idx_edges_type ON edges(edge_type);
            CREATE INDEX idx_edges_package ON edges(package_key);
            CREATE INDEX idx_evidence_target ON evidence(target_type, target_id);
            CREATE INDEX idx_evidence_path ON evidence(source_path);
            CREATE INDEX idx_comments_owner ON comments(owner_node_id);
            CREATE INDEX idx_issues_source ON issues(source_node_id);
            CREATE INDEX idx_issues_package ON issues(package_key);
            CREATE INDEX idx_issues_type ON issues(issue_type, severity);
            CREATE TRIGGER evidence_node_target_insert
            BEFORE INSERT ON evidence
            WHEN NEW.target_type = 'NODE'
              AND NOT EXISTS (SELECT 1 FROM nodes WHERE node_id = NEW.target_id)
            BEGIN
                SELECT RAISE(ABORT, 'evidence node target does not exist');
            END;
            CREATE TRIGGER evidence_edge_target_insert
            BEFORE INSERT ON evidence
            WHEN NEW.target_type = 'EDGE'
              AND NOT EXISTS (SELECT 1 FROM edges WHERE edge_id = NEW.target_id)
            BEGIN
                SELECT RAISE(ABORT, 'evidence edge target does not exist');
            END;
            PRAGMA user_version = 2;
            """
        )
        connection.executemany(
            "INSERT INTO metadata(key, value_json) VALUES (?, ?)",
            [
                ("contract_version", json.dumps("2.0")),
                ("generated_at", json.dumps(generated_at)),
                ("source_name", json.dumps(source_name)),
                ("identity", json.dumps("deterministic-63-bit")),
                ("output_mode", json.dumps(output_mode)),
                ("markdown_options", json.dumps(markdown_options)),
                (
                    "source_descriptors",
                    json.dumps(graph.source_descriptors, ensure_ascii=False),
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.source_key,
                    record.relative_path,
                    record.source_type,
                    record.system_key,
                    record.repository_key,
                    record.declared_encoding,
                    record.actual_encoding,
                    record.raw_sha256,
                    record.text_sha256,
                    record.newline_style,
                    record.bom,
                )
                for record in graph.source_records
            ],
        )
        connection.executemany(
            """
            INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["node_id"]),
                    row["stable_id"],
                    row["node_type"],
                    row["technical_name"],
                    row["qualified_name"],
                    row["default_display_name"],
                    row["system_key"],
                    row["database_key"],
                    row["repository_key"],
                    row["graph_role"],
                    float(row["confidence"]),
                    row["properties_json"],
                    node_packages.get(row["node_id"], "global"),
                )
                for row in rows["nodes"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["edge_id"]),
                    row["stable_id"],
                    int(row["source_node_id"]),
                    int(row["target_node_id"]),
                    row["edge_type"],
                    row["graph_layer"],
                    row["raw_operation"],
                    float(row["confidence"]),
                    row["properties_json"],
                    edge_packages.get(row["edge_id"], "global"),
                )
                for row in rows["edges"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["evidence_id"]),
                    row["stable_id"],
                    row["target_type"],
                    int(row["target_id"]),
                    row["source_path"],
                    _optional_sqlite_int(row.get("start_line")),
                    _optional_sqlite_int(row.get("end_line")),
                    _optional_sqlite_int(row.get("start_column")),
                    _optional_sqlite_int(row.get("end_column")),
                    row["evidence_kind"],
                    row["extractor_name"],
                    float(row["confidence"]),
                    row["snippet"],
                    row["properties_json"],
                    evidence_packages.get(row["evidence_id"], "global"),
                )
                for row in rows["evidence"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO comments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["comment_id"]),
                    row["stable_id"],
                    row["source_path"],
                    int(row["owner_node_id"]),
                    row["comment_kind"],
                    _optional_sqlite_int(row.get("start_line")),
                    _optional_sqlite_int(row.get("end_line")),
                    _optional_sqlite_int(row.get("start_column")),
                    _optional_sqlite_int(row.get("end_column")),
                    row["raw_text"],
                    row["normalized_text"],
                    row["language"],
                    row["encoding"],
                    row["properties_json"],
                    comment_packages.get(row["comment_id"], "global"),
                )
                for row in rows["comments"]
            ],
        )
        connection.executemany(
            """
            INSERT INTO issues VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(row["issue_id"]),
                    row["stable_id"],
                    row["issue_type"],
                    row["severity"],
                    _optional_sqlite_int(row.get("source_node_id")),
                    row["raw_reference"],
                    row["database_key"],
                    row["source_path"],
                    _optional_sqlite_int(row.get("start_line")),
                    row["message"],
                    row["properties_json"],
                    issue_packages.get(row["issue_id"], "global"),
                )
                for row in rows["issues"]
            ],
        )
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise ValueError(f"SQLite foreign key validation failed: {violations[:5]}")
        connection.execute("ANALYZE")
        connection.commit()


def _optional_sqlite_int(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def _graph_rows(
    graph: GraphPackage,
    *,
    max_evidence_snippet_chars: int,
    max_issues_per_type_per_file: int,
) -> dict[str, list[dict[str, str]]]:
    result = {
        name: [dict(row) for row in getattr(graph, name).values()]
        for name in CSV_HEADERS
    }
    for evidence in result["evidence"]:
        snippet = evidence.get("snippet", "")
        if len(snippet) > max_evidence_snippet_chars:
            evidence["snippet"] = (
                snippet[: max_evidence_snippet_chars - 1] + "…"
                if max_evidence_snippet_chars > 1
                else "…"
            )
    result["issues"] = _bounded_issues(
        result["issues"], max_issues_per_type_per_file
    )
    for name, headers in CSV_HEADERS.items():
        result[name].sort(key=lambda row: row.get(headers[0], ""))
    return result


def _bounded_issues(
    issues: list[dict[str, str]], maximum: int
) -> list[dict[str, str]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for issue in sorted(issues, key=lambda row: row.get("issue_id", "")):
        grouped[
            (issue.get("source_path", ""), issue.get("issue_type", ""))
        ].append(issue)
    result: list[dict[str, str]] = []
    for (source_path, issue_type), values in sorted(grouped.items()):
        if len(values) <= maximum:
            result.extend(values)
            continue
        retained = values[: max(maximum - 1, 0)]
        omitted = values[len(retained) :]
        digest = hashlib.sha256(
            "|".join(item.get("issue_id", "") for item in omitted).encode("utf-8")
        ).hexdigest()[:24]
        retained.append(
            {
                "issue_id": f"issue:{digest}",
                "issue_type": issue_type,
                "severity": "WARNING",
                "source_node_id": "",
                "raw_reference": "",
                "database_key": "",
                "source_path": source_path,
                "start_line": "",
                "message": f"{len(omitted)} additional {issue_type} issues omitted by configured limit",
                "properties_json": _json(
                    {
                        "omitted_count": len(omitted),
                        "limit": maximum,
                        "summary": True,
                    }
                ),
            }
        )
        result.extend(retained)
    return result


def _partition_graph(
    graph: GraphPackage, rows: dict[str, list[dict[str, str]]]
) -> dict[str, object]:
    source_names = sorted(
        {
            *graph.source_descriptors,
            *(record.source_key for record in graph.source_records),
        }
    )
    path_sources: dict[str, set[str]] = defaultdict(set)
    repository_sources: dict[str, set[str]] = defaultdict(set)
    system_sources: dict[str, set[str]] = defaultdict(set)
    for record in graph.source_records:
        path_sources[record.relative_path].add(record.source_key)
        repository_sources[record.repository_key].add(record.source_key)
        system_sources[record.system_key].add(record.source_key)
    for source_key, descriptor in graph.source_descriptors.items():
        repository_sources[descriptor["repository_key"]].add(source_key)
        system_sources[descriptor["system_key"]].add(source_key)

    node_source: dict[str, str] = {}
    for node_id, node in graph.nodes.items():
        properties = _properties(node)
        configured = str(
            properties.get("source_key")
            or properties.get("source_id")
            or properties.get("source")
            or ""
        )
        if configured in source_names:
            node_source[node_id] = configured
            continue
        stable_id = str(node.get("stable_id") or node_id)
        if stable_id.startswith("file:"):
            candidate = stable_id.split(":", 2)[1]
            if candidate in source_names:
                node_source[node_id] = candidate
                continue
        repository = node.get("repository_key", "")
        candidates = repository_sources.get(repository, set())
        if len(candidates) == 1:
            node_source[node_id] = next(iter(candidates))
            continue
        system = node.get("system_key", "")
        candidates = system_sources.get(system, set())
        if len(candidates) == 1:
            node_source[node_id] = next(iter(candidates))

    for evidence in graph.evidence.values():
        if evidence.get("target_type") != "NODE":
            continue
        node_id = evidence.get("target_id", "")
        candidates = path_sources.get(evidence.get("source_path", ""), set())
        if node_id and len(candidates) == 1:
            node_source.setdefault(node_id, next(iter(candidates)))

    propagation_edges = {
        "CONTAINS",
        "HANDLED_BY",
        "HANDLES_API",
        "ENTRY_IN",
        "BELONGS_TO",
        "PROJECT_REFERENCE",
        "DEFINES_STATEMENT",
        "INCLUDES_FRAGMENT",
        "EXECUTES_SQL",
    }
    changed = True
    while changed:
        changed = False
        for edge in graph.edges.values():
            if edge.get("edge_type") not in propagation_edges:
                continue
            source = edge.get("source_node_id", "")
            target = edge.get("target_node_id", "")
            source_key = node_source.get(source)
            target_key = node_source.get(target)
            if source_key and not target_key:
                node_source[target] = source_key
                changed = True
            elif target_key and not source_key:
                node_source[source] = target_key
                changed = True

    packages = {
        source: {name: [] for name in CSV_HEADERS} for source in source_names
    }
    global_rows = {name: [] for name in CSV_HEADERS}
    node_packages: dict[str, str] = {}
    edge_packages: dict[str, str] = {}
    evidence_packages: dict[str, str] = {}
    comment_packages: dict[str, str] = {}
    issue_packages: dict[str, str] = {}

    for row in rows["nodes"]:
        node_id = row["node_id"]
        source = node_source.get(node_id)
        if source:
            packages[source]["nodes"].append(row)
            node_packages[node_id] = f"sources/{source}"
        else:
            global_rows["nodes"].append(row)
            node_packages[node_id] = "global"

    edge_source: dict[str, str | None] = {}
    for row in rows["edges"]:
        edge_id = row["edge_id"]
        source = node_source.get(row.get("source_node_id", ""))
        target = node_source.get(row.get("target_node_id", ""))
        owner = source if source and source == target else None
        edge_source[edge_id] = owner
        if owner:
            packages[owner]["edges"].append(row)
            edge_packages[edge_id] = f"sources/{owner}"
        else:
            global_rows["edges"].append(row)
            edge_packages[edge_id] = "global"

    for row in rows["evidence"]:
        evidence_id = row["evidence_id"]
        if row.get("target_type") == "NODE":
            owner = node_source.get(row.get("target_id", ""))
        else:
            owner = edge_source.get(row.get("target_id", ""))
        if not owner:
            candidates = path_sources.get(row.get("source_path", ""), set())
            owner = next(iter(candidates)) if len(candidates) == 1 else None
        if owner:
            packages[owner]["evidence"].append(row)
            evidence_packages[evidence_id] = f"sources/{owner}"
        else:
            global_rows["evidence"].append(row)
            evidence_packages[evidence_id] = "global"

    for row in rows["comments"]:
        owner = node_source.get(row.get("owner_node_id", ""))
        if not owner:
            candidates = path_sources.get(row.get("source_path", ""), set())
            owner = next(iter(candidates)) if len(candidates) == 1 else None
        if owner:
            packages[owner]["comments"].append(row)
            comment_packages[row["comment_id"]] = f"sources/{owner}"
        else:
            global_rows["comments"].append(row)
            comment_packages[row["comment_id"]] = "global"

    for row in rows["issues"]:
        owner = node_source.get(row.get("source_node_id", ""))
        if not owner:
            properties = _properties(row)
            owner = node_source.get(str(properties.get("source_node_id") or ""))
            configured_source = str(properties.get("source_key") or "")
            if not owner and configured_source in packages:
                owner = configured_source
        if not owner:
            candidates = path_sources.get(row.get("source_path", ""), set())
            owner = next(iter(candidates)) if len(candidates) == 1 else None
        if owner:
            packages[owner]["issues"].append(row)
            issue_packages[row["issue_id"]] = f"sources/{owner}"
        else:
            global_rows["issues"].append(row)
            issue_packages[row["issue_id"]] = "global"

    return {
        "sources": packages,
        "global": global_rows,
        "node_packages": node_packages,
        "edge_packages": edge_packages,
        "evidence_packages": evidence_packages,
        "comment_packages": comment_packages,
        "issue_packages": issue_packages,
    }


def write_codebase_memory(
    graph: GraphPackage,
    output: Path,
    *,
    generated_at: str | None = None,
    knowledge_refs: dict[str, list[str]] | None = None,
    write_summaries: bool = True,
) -> dict[str, dict[str, object]]:
    """Write machine-readable memory and its optional Markdown summaries."""
    return _write_codebase_memory(
        graph,
        output,
        generated_at=generated_at or _utc_now(),
        knowledge_refs=knowledge_refs or {},
        write_summaries=write_summaries,
    )


def _write_codebase_memory(
    graph: GraphPackage,
    output: Path,
    *,
    generated_at: str,
    knowledge_refs: dict[str, list[str]],
    write_summaries: bool = True,
) -> dict[str, dict[str, object]]:
    memory_dir = output / "codebase-memory"
    entities_dir = memory_dir / "entities"
    relationships_dir = memory_dir / "relationships"
    summaries_dir = memory_dir / "summaries"
    directories = [entities_dir, relationships_dir]
    if write_summaries:
        directories.append(summaries_dir)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    outgoing, incoming, evidence_by_target, issues_by_node = _graph_indexes(graph)
    entity_groups: dict[str, list[dict[str, object]]] = {
        "apis": [],
        "tables": [],
        "procedures": [],
        "jobs": [],
        "modules": [],
    }
    for node_id, node in sorted(graph.nodes.items()):
        group = (
            "tables"
            if _is_database_node(node)
            else _memory_entity_group(node.get("node_type", ""))
        )
        if group is None:
            continue
        related = {
            edge.get("target_node_id", "")
            for edge in outgoing.get(node_id, ())
        } | {
            edge.get("source_node_id", "")
            for edge in incoming.get(node_id, ())
        }
        evidence_ids = [
            row["evidence_id"] for row in evidence_by_target.get(node_id, ())
        ]
        issue_ids = issues_by_node.get(node_id, [])
        record: dict[str, object] = {
            "memory_id": f"memory:node:{node_id}",
            "node_id": node_id,
            "kind": node.get("node_type", ""),
            "source": _node_source_label(node),
            "qualified_name": node.get("qualified_name", ""),
            "display_name": node.get("default_display_name", ""),
            "summary": _node_summary(
                node,
                related_count=len(related),
                issue_count=len(issue_ids),
            ),
            "evidence_ids": sorted(evidence_ids)[:100],
            "related_node_ids": sorted(item for item in related if item)[:100],
            "issue_ids": sorted(issue_ids)[:100],
            "knowledge_refs": sorted(knowledge_refs.get(node_id, ())),
            "updated_at": generated_at,
        }
        record["content_hash"] = _content_hash(record, excluded={"updated_at"})
        entity_groups[group].append(record)

    memory_refs: dict[str, dict[str, object]] = {}
    entity_files: list[str] = []
    for group, records in entity_groups.items():
        relative = f"entities/{group}.jsonl"
        entity_files.append(relative)
        with (memory_dir / relative).open("w", encoding="utf-8") as handle:
            for line, record in enumerate(records, 1):
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                memory_refs[str(record["node_id"])] = {
                    "path": f"codebase-memory/{relative}",
                    "line": line,
                    "memoryId": record["memory_id"],
                }

    relationship_groups = _memory_relationships(
        graph,
        evidence_by_target=evidence_by_target,
        issues_by_node=issues_by_node,
    )
    relationship_files: list[str] = []
    for group, records in relationship_groups.items():
        relative = f"relationships/{group}.jsonl"
        relationship_files.append(relative)
        with (memory_dir / relative).open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    summary_files = (
        _write_memory_summaries(memory_dir, entity_groups, relationship_groups)
        if write_summaries
        else []
    )
    manifest = {
        "version": "1.0",
        "generatedAt": generated_at,
        "files": {
            "entities": entity_files,
            "relationships": relationship_files,
            "summaries": summary_files,
        },
        "statistics": {
            "entities": sum(len(rows) for rows in entity_groups.values()),
            "relationships": sum(
                len(rows) for rows in relationship_groups.values()
            ),
        },
    }
    (memory_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return memory_refs


def _graph_indexes(
    graph: GraphPackage,
) -> tuple[
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
    dict[str, list[str]],
]:
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    incoming: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in graph.edges.values():
        outgoing[edge.get("source_node_id", "")].append(edge)
        incoming[edge.get("target_node_id", "")].append(edge)
    evidence_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    nodes_by_path: dict[str, set[str]] = defaultdict(set)
    for evidence in graph.evidence.values():
        target_id = evidence.get("target_id", "")
        evidence_by_target[target_id].append(evidence)
        if evidence.get("target_type") == "NODE":
            nodes_by_path[evidence.get("source_path", "")].add(target_id)
    issues_by_node: dict[str, list[str]] = defaultdict(list)
    for issue_id, issue in graph.issues.items():
        owners = set()
        if issue.get("source_node_id"):
            owners.add(issue["source_node_id"])
        properties = _properties(issue)
        if properties.get("source_node_id"):
            owners.add(str(properties["source_node_id"]))
        owners.update(nodes_by_path.get(issue.get("source_path", ""), ()))
        for owner in owners:
            issues_by_node[owner].append(issue_id)
    for values in outgoing.values():
        values.sort(key=lambda row: row.get("edge_id", ""))
    for values in incoming.values():
        values.sort(key=lambda row: row.get("edge_id", ""))
    for values in evidence_by_target.values():
        values.sort(key=lambda row: row.get("evidence_id", ""))
    return outgoing, incoming, evidence_by_target, issues_by_node


def _memory_entity_group(node_type: str) -> str | None:
    if node_type == "API_OPERATION":
        return "apis"
    if node_type in {
        "TABLE",
        "VIEW",
        "MATERIALIZED_VIEW",
        "EXTERNAL_DATABASE_OBJECT",
        "COLUMN",
    }:
        return "tables"
    if node_type in {
        "PROCEDURE",
        "FUNCTION",
        "LOCAL_ROUTINE",
        "PLSQL_PACKAGE",
        "PACKAGE",
    }:
        return "procedures"
    if node_type in {
        "JOB",
        "JOB_NETWORK",
        "EXECUTABLE",
        "EXECUTABLE_ENTRY_POINT",
        "LOADER_CONTROL",
    }:
        return "jobs"
    if node_type in {
        "FILE",
        "DATA_FILE",
        "EXTERNAL_DATABASE_OBJECT",
        "UNRESOLVED_REFERENCE",
    }:
        return None
    if node_type in {
        "SYSTEM",
        "APPLICATION",
        "API_APPLICATION",
        "PROJECT",
        "ANGULAR_PROJECT",
        "DOTNET_PROJECT",
        "DOTNET_SOLUTION",
        "ASSEMBLY",
        "NAMESPACE",
        "COMPONENT",
        "ANGULAR_COMPONENT",
        "SERVICE",
        "ANGULAR_SERVICE",
        "REPOSITORY",
        "CONTROLLER",
        "CLASS",
        "CSHARP_TYPE",
        "INTERFACE",
        "XML_SQL_MAPPER",
        "PLSQL_PACKAGE",
        "PACKAGE",
    }:
        return "modules"
    return None


def _node_source_label(node: dict[str, str]) -> str:
    properties = _properties(node)
    return str(
        properties.get("source_key")
        or properties.get("source_id")
        or node.get("repository_key")
        or node.get("system_key")
        or ""
    )


def _node_summary(
    node: dict[str, str], *, related_count: int, issue_count: int
) -> str:
    kind = node.get("node_type", "node").replace("_", " ").lower()
    name = (
        node.get("default_display_name")
        or node.get("qualified_name")
        or node.get("technical_name")
        or node.get("node_id")
        or ""
    )
    suffix = f" Linked to {related_count} node(s)."
    if issue_count:
        suffix += f" Has {issue_count} extractor issue(s)."
    return f"{kind.title()} {name}.{suffix}"


_TRACE_EDGE_TYPES = frozenset(
    {
        "CALLS",
        "CALLS_API",
        "HANDLED_BY",
        "HANDLES_API",
        "CONTAINS",
        "ENTRY_IN",
        "RESOLVES_TO",
        "STARTS",
        "TRIGGERS",
        "TRIGGERS_JOB",
        "EXECUTES_SQL",
        "READS",
        "REMOTE_READS",
        "READS_FROM",
        "INSERTS",
        "UPDATES",
        "DELETES",
        "MERGES",
        "WRITES",
        "WRITES_TO",
        "LOADS_INTO",
    }
)
_DB_NODE_TYPES = frozenset(
    {"TABLE", "VIEW", "MATERIALIZED_VIEW", "EXTERNAL_DATABASE_OBJECT"}
)
_UI_NODE_TYPES = frozenset(
    {
        "SCREEN",
        "ROUTE",
        "ANGULAR_COMPONENT",
        "COMPONENT",
        "UI_ACTION",
        "API_CALL_REFERENCE",
        "API_CLIENT_CALL",
    }
)
_JOB_NODE_TYPES = frozenset(
    {"JOB", "JOB_NETWORK", "EXECUTABLE", "EXECUTABLE_ENTRY_POINT", "LOADER_CONTROL"}
)
_READ_EDGE_TYPES = frozenset({"READS", "REMOTE_READS", "READS_FROM"})
_WRITE_EDGE_TYPES = frozenset(
    {
        "INSERTS",
        "UPDATES",
        "DELETES",
        "MERGES",
        "WRITES",
        "WRITES_TO",
        "LOADS_INTO",
    }
)


def _trace_outgoing(
    graph: GraphPackage,
) -> dict[str, list[dict[str, str]]]:
    outgoing: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge in graph.edges.values():
        if edge.get("edge_type") in _TRACE_EDGE_TYPES:
            outgoing[edge.get("source_node_id", "")].append(edge)
    return outgoing


def _shortest_paths_to_types(
    graph: GraphPackage,
    start_node_id: str,
    target_types: frozenset[str],
    *,
    max_depth: int = 8,
    outgoing_index: dict[str, list[dict[str, str]]] | None = None,
) -> list[tuple[str, list[str], list[str]]]:
    outgoing = (
        outgoing_index
        if outgoing_index is not None
        else _trace_outgoing(graph)
    )
    queue: deque[tuple[str, list[str], list[str]]] = deque(
        [(start_node_id, [start_node_id], [])]
    )
    best_depth: dict[str, int] = {start_node_id: 0}
    results: dict[str, tuple[str, list[str], list[str]]] = {}
    while queue:
        current, path_nodes, path_edges = queue.popleft()
        if len(path_edges) >= max_depth:
            continue
        for edge in outgoing.get(current, ()):
            target = edge.get("target_node_id", "")
            if not target or target in path_nodes:
                continue
            next_nodes = [*path_nodes, target]
            next_edges = [*path_edges, edge["edge_id"]]
            target_node = graph.nodes.get(target)
            if target_node and _matches_target_type(target_node, target_types):
                results.setdefault(target, (target, next_nodes, next_edges))
                continue
            depth = len(next_edges)
            if depth < best_depth.get(target, max_depth + 1):
                best_depth[target] = depth
                queue.append((target, next_nodes, next_edges))
    return [results[key] for key in sorted(results)]


def _memory_relationships(
    graph: GraphPackage,
    *,
    evidence_by_target: dict[str, list[dict[str, str]]],
    issues_by_node: dict[str, list[str]],
) -> dict[str, list[dict[str, object]]]:
    groups: dict[str, list[dict[str, object]]] = {
        "api-to-db": [],
        "ui-to-api": [],
        "job-to-db": [],
        "unresolved": [],
    }
    trace_outgoing = _trace_outgoing(graph)
    for node_id, node in sorted(graph.nodes.items()):
        node_type = node.get("node_type", "")
        if node_type == "API_OPERATION":
            for target_id, path_nodes, edge_ids in _shortest_paths_to_types(
                graph,
                node_id,
                _DB_NODE_TYPES,
                outgoing_index=trace_outgoing,
            ):
                groups["api-to-db"].append(
                    _relationship_record(
                        graph,
                        kind="API_TO_DB",
                        source=node_id,
                        target=target_id,
                        path_nodes=path_nodes,
                        edge_ids=edge_ids,
                        evidence_by_target=evidence_by_target,
                        issues_by_node=issues_by_node,
                    )
                )
        if node_type in _UI_NODE_TYPES:
            for target_id, path_nodes, edge_ids in _shortest_paths_to_types(
                graph,
                node_id,
                frozenset({"API_OPERATION"}),
                max_depth=6,
                outgoing_index=trace_outgoing,
            ):
                groups["ui-to-api"].append(
                    _relationship_record(
                        graph,
                        kind="UI_TO_API",
                        source=node_id,
                        target=target_id,
                        path_nodes=path_nodes,
                        edge_ids=edge_ids,
                        evidence_by_target=evidence_by_target,
                        issues_by_node=issues_by_node,
                    )
                )
        if node_type in _JOB_NODE_TYPES:
            for target_id, path_nodes, edge_ids in _shortest_paths_to_types(
                graph,
                node_id,
                _DB_NODE_TYPES,
                outgoing_index=trace_outgoing,
            ):
                groups["job-to-db"].append(
                    _relationship_record(
                        graph,
                        kind="JOB_TO_DB",
                        source=node_id,
                        target=target_id,
                        path_nodes=path_nodes,
                        edge_ids=edge_ids,
                        evidence_by_target=evidence_by_target,
                        issues_by_node=issues_by_node,
                    )
                )

    for edge_id, edge in sorted(graph.edges.items()):
        target = graph.nodes.get(edge.get("target_node_id", ""))
        if not target or target.get("node_type") not in {
            "UNRESOLVED_REFERENCE",
            "EXTERNAL_DATABASE_OBJECT",
        }:
            continue
        groups["unresolved"].append(
            _relationship_record(
                graph,
                kind="UNRESOLVED",
                source=edge.get("source_node_id", ""),
                target=edge.get("target_node_id", ""),
                path_nodes=[
                    edge.get("source_node_id", ""),
                    edge.get("target_node_id", ""),
                ],
                edge_ids=[edge_id],
                evidence_by_target=evidence_by_target,
                issues_by_node=issues_by_node,
            )
        )
    for rows in groups.values():
        rows.sort(key=lambda row: str(row["memory_id"]))
    return groups


def _relationship_record(
    graph: GraphPackage,
    *,
    kind: str,
    source: str,
    target: str,
    path_nodes: list[str],
    edge_ids: list[str],
    evidence_by_target: dict[str, list[dict[str, str]]],
    issues_by_node: dict[str, list[str]],
) -> dict[str, object]:
    evidence_ids = {
        row["evidence_id"]
        for item in [*path_nodes, *edge_ids]
        for row in evidence_by_target.get(item, ())
    }
    issue_ids = {
        issue_id for node_id in path_nodes for issue_id in issues_by_node.get(node_id, ())
    }
    confidences = [
        float(graph.edges[edge_id].get("confidence") or 0.0)
        for edge_id in edge_ids
        if edge_id in graph.edges
    ]
    source_name = _display_name(graph.nodes.get(source, {}), source)
    target_name = _display_name(graph.nodes.get(target, {}), target)
    identity = "|".join((kind, source, target, *edge_ids))
    return {
        "memory_id": "memory:relationship:"
        + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24],
        "edge_ids": edge_ids,
        "kind": kind,
        "from_node_id": source,
        "to_node_id": target,
        "path_node_ids": path_nodes,
        "summary": f"{source_name} reaches {target_name} through {len(edge_ids)} edge(s).",
        "confidence": min(confidences) if confidences else 0.2,
        "evidence_ids": sorted(evidence_ids)[:100],
        "issue_ids": sorted(issue_ids)[:100],
    }


def _write_memory_summaries(
    memory_dir: Path,
    entities: dict[str, list[dict[str, object]]],
    relationships: dict[str, list[dict[str, object]]],
) -> list[str]:
    cards = {
        "summaries/api-cards.md": (
            "API cards",
            [str(row["summary"]) for row in entities["apis"]],
        ),
        "summaries/db-cards.md": (
            "Database cards",
            [str(row["summary"]) for row in entities["tables"]],
        ),
        "summaries/flow-cards.md": (
            "Flow cards",
            [
                str(row["summary"])
                for name in ("ui-to-api", "api-to-db", "job-to-db")
                for row in relationships[name]
            ],
        ),
        "summaries/issue-cards.md": (
            "Unresolved cards",
            [str(row["summary"]) for row in relationships["unresolved"]],
        ),
    }
    for relative, (title, values) in cards.items():
        lines = [f"# {title}", ""]
        lines.extend(f"- {value}" for value in values)
        if not values:
            lines.append("- No cards generated.")
        (memory_dir / relative).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return list(cards)


def write_knowledge_markdown(
    graph: GraphPackage,
    output: Path,
    *,
    generated_at: str | None = None,
    chunking: dict[str, object] | None = None,
) -> dict[str, list[str]]:
    """Write the RAG Markdown projection for an already extracted graph."""
    return _write_knowledge(
        graph,
        output,
        generated_at=generated_at or _utc_now(),
        chunking=chunking or {},
    )


def _write_knowledge(
    graph: GraphPackage,
    output: Path,
    *,
    generated_at: str,
    chunking: dict[str, object],
) -> dict[str, list[str]]:
    knowledge_dir = output / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    outgoing, incoming, evidence_by_target, issues_by_node = _graph_indexes(graph)
    topics: dict[str, list[dict[str, object]]] = {
        "APIs": [],
        "Flows": [],
        "Databases": [],
        "Jobs": [],
        "CrossSystem": [],
    }

    for node_id, node in sorted(graph.nodes.items()):
        node_type = node.get("node_type", "")
        if node_type == "API_OPERATION":
            db_paths = _shortest_paths_to_types(
                graph,
                node_id,
                _DB_NODE_TYPES,
                outgoing_index=outgoing,
            )
            callers = [
                edge.get("source_node_id", "")
                for edge in incoming.get(node_id, ())
                if edge.get("edge_type") == "CALLS_API"
            ]
            related = {
                edge.get("target_node_id", "")
                for edge in outgoing.get(node_id, ())
            } | set(callers) | {path[0] for path in db_paths}
            edge_ids = {
                edge["edge_id"] for edge in outgoing.get(node_id, ())
            } | {
                edge["edge_id"] for edge in incoming.get(node_id, ())
            } | {
                edge_id for _, _, path_edges in db_paths for edge_id in path_edges
            }
            related_issues = {
                issue_id
                for related_id in {
                    node_id,
                    *callers,
                    *(
                        path_node
                        for _, path_nodes, _ in db_paths
                        for path_node in path_nodes
                    ),
                }
                for issue_id in issues_by_node.get(related_id, ())
            }
            topics["APIs"].append(
                _knowledge_card(
                    graph,
                    node_id=node_id,
                    heading=_api_heading(node),
                    related_node_ids=related,
                    edge_ids=edge_ids,
                    evidence_by_target=evidence_by_target,
                    issue_ids=related_issues,
                    narrative=_api_narrative(graph, node_id, callers, db_paths),
                )
            )
        elif node_type in _UI_NODE_TYPES:
            api_paths = _shortest_paths_to_types(
                graph,
                node_id,
                frozenset({"API_OPERATION"}),
                max_depth=6,
                outgoing_index=outgoing,
            )
            db_paths = _shortest_paths_to_types(
                graph,
                node_id,
                _DB_NODE_TYPES,
                outgoing_index=outgoing,
            )
            related = {path[0] for path in [*api_paths, *db_paths]}
            edge_ids = {
                edge_id
                for _, _, path_edges in [*api_paths, *db_paths]
                for edge_id in path_edges
            }
            related_issues = {
                issue_id
                for related_id in {
                    node_id,
                    *(
                        path_node
                        for _, path_nodes, _ in [*api_paths, *db_paths]
                        for path_node in path_nodes
                    ),
                }
                for issue_id in issues_by_node.get(related_id, ())
            }
            topics["Flows"].append(
                _knowledge_card(
                    graph,
                    node_id=node_id,
                    heading=_display_name(node, node_id),
                    related_node_ids=related,
                    edge_ids=edge_ids,
                    evidence_by_target=evidence_by_target,
                    issue_ids=related_issues,
                    narrative=_flow_narrative(graph, api_paths, db_paths),
                )
            )
        elif _is_database_node(node):
            reader_edges = [
                edge
                for edge in incoming.get(node_id, ())
                if edge.get("edge_type") in _READ_EDGE_TYPES
            ]
            writer_edges = [
                edge
                for edge in incoming.get(node_id, ())
                if edge.get("edge_type") in _WRITE_EDGE_TYPES
            ]
            related = {
                edge.get("source_node_id", "")
                for edge in [*reader_edges, *writer_edges]
            }
            related_issues = {
                issue_id
                for related_id in {node_id, *related}
                for issue_id in issues_by_node.get(related_id, ())
            }
            topics["Databases"].append(
                _knowledge_card(
                    graph,
                    node_id=node_id,
                    heading=_display_name(node, node_id),
                    related_node_ids=related,
                    edge_ids={
                        edge["edge_id"] for edge in [*reader_edges, *writer_edges]
                    },
                    evidence_by_target=evidence_by_target,
                    issue_ids=related_issues,
                    narrative=_database_narrative(
                        graph, reader_edges, writer_edges
                    ),
                )
            )
        elif node_type in _JOB_NODE_TYPES:
            db_paths = _shortest_paths_to_types(
                graph,
                node_id,
                _DB_NODE_TYPES,
                outgoing_index=outgoing,
            )
            related = {path[0] for path in db_paths}
            edge_ids = {
                edge_id
                for _, _, path_edges in db_paths
                for edge_id in path_edges
            }
            related_issues = {
                issue_id
                for related_id in {
                    node_id,
                    *(
                        path_node
                        for _, path_nodes, _ in db_paths
                        for path_node in path_nodes
                    ),
                }
                for issue_id in issues_by_node.get(related_id, ())
            }
            topics["Jobs"].append(
                _knowledge_card(
                    graph,
                    node_id=node_id,
                    heading=_display_name(node, node_id),
                    related_node_ids=related,
                    edge_ids=edge_ids,
                    evidence_by_target=evidence_by_target,
                    issue_ids=related_issues,
                    narrative=_job_narrative(graph, db_paths),
                )
            )

    for edge_id, edge in sorted(graph.edges.items()):
        source_id = edge.get("source_node_id", "")
        target_id = edge.get("target_node_id", "")
        source = graph.nodes.get(source_id)
        target = graph.nodes.get(target_id)
        if not source or not target:
            continue
        source_label = _node_source_label(source)
        target_label = _node_source_label(target)
        if source_label == target_label and target.get("node_type") not in {
            "UNRESOLVED_REFERENCE",
            "EXTERNAL_DATABASE_OBJECT",
        }:
            continue
        heading = (
            f"{_display_name(source, source_id)} → "
            f"{_display_name(target, target_id)}"
        )
        topics["CrossSystem"].append(
            _knowledge_card(
                graph,
                node_id=source_id,
                heading=heading,
                related_node_ids={target_id},
                edge_ids={edge_id},
                evidence_by_target=evidence_by_target,
                issue_ids={
                    *issues_by_node.get(source_id, ()),
                    *issues_by_node.get(target_id, ()),
                },
                narrative=(
                    f"`{source.get('qualified_name', source_id)}` "
                    f"{edge.get('edge_type', '').lower().replace('_', ' ')} "
                    f"`{target.get('qualified_name', target_id)}`."
                ),
                reference_node_ids={source_id, target_id},
            )
        )

    enabled = chunking.get("enabled", True) is not False
    max_bytes = _coerce_positive_int(
        chunking.get("maxMarkdownBytes"), 1024 * 1024, minimum=1024
    )
    split_by = {
        str(value)
        for value in (
            chunking.get("splitBy", [])
            if isinstance(chunking.get("splitBy", []), list)
            else []
        )
    }
    references: dict[str, list[str]] = defaultdict(list)
    descriptors: list[dict[str, object]] = []
    for topic, cards in topics.items():
        written, topic_refs = _write_topic_chunks(
            knowledge_dir,
            topic,
            cards,
            enabled=enabled,
            max_bytes=max_bytes,
            split_by=split_by,
        )
        descriptors.extend(written)
        for node_id, values in topic_refs.items():
            references[node_id].extend(values)
    manifest = {
        "version": "1.0",
        "generatedAt": generated_at,
        "chunking": {
            "enabled": enabled,
            "maxMarkdownBytes": max_bytes,
            "splitBy": sorted(split_by),
        },
        "files": descriptors,
    }
    (knowledge_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        node_id: sorted(set(values)) for node_id, values in references.items()
    }


def _knowledge_card(
    graph: GraphPackage,
    *,
    node_id: str,
    heading: str,
    related_node_ids: Iterable[str],
    edge_ids: Iterable[str],
    evidence_by_target: dict[str, list[dict[str, str]]],
    issue_ids: Iterable[str],
    narrative: str,
    reference_node_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    node = graph.nodes[node_id]
    related = sorted({item for item in related_node_ids if item})[:50]
    edges = sorted(set(edge_ids))[:100]
    evidence = {
        row["evidence_id"] for row in evidence_by_target.get(node_id, ())
    }
    for edge_id in edges:
        evidence.update(
            row["evidence_id"] for row in evidence_by_target.get(edge_id, ())
        )
    issues = sorted(set(issue_ids))[:50]
    metadata_lines = [
        f"node_id: {json.dumps(node_id, ensure_ascii=False)}",
        f"source: {json.dumps(_node_source_label(node), ensure_ascii=False)}",
        f"kind: {json.dumps(node.get('node_type', ''), ensure_ascii=False)}",
        *_yaml_list("related_nodes", related),
        *_yaml_list("edge_ids", edges),
        *_yaml_list("evidence_ids", sorted(evidence)[:100]),
        *_yaml_list("issues", issues),
    ]
    safe_heading = " ".join(str(heading).replace("\n", " ").split()) or node_id
    text = (
        f"## {safe_heading}\n\n```yaml\n"
        + "\n".join(metadata_lines)
        + f"\n```\n\n{narrative.strip()}\n\n"
    )
    return {
        "text": text,
        "source": _node_source_label(node),
        "nodeType": node.get("node_type", ""),
        "anchor": _markdown_anchor(safe_heading),
        "nodeIds": sorted(
            set(reference_node_ids or {node_id})
        ),
    }


def _yaml_list(name: str, values: Iterable[str]) -> list[str]:
    items = list(values)
    if not items:
        return [f"{name}: []"]
    return [f"{name}:", *(f"  - {json.dumps(item, ensure_ascii=False)}" for item in items)]


def _write_topic_chunks(
    output: Path,
    topic: str,
    cards: list[dict[str, object]],
    *,
    enabled: bool,
    max_bytes: int,
    split_by: set[str],
) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for card in cards:
        key = str(card.get("source") or "global") if "source" in split_by else ""
        grouped[key].append(card)
    if not grouped:
        grouped[""] = []
    descriptors: list[dict[str, object]] = []
    references: dict[str, list[str]] = defaultdict(list)
    for group, values in sorted(grouped.items()):
        values.sort(
            key=lambda card: (
                str(card.get("nodeType", "")),
                str(card.get("anchor", "")),
            )
        )
        header = f"# {topic}\n\n"
        chunks: list[list[dict[str, object]]] = [[]]
        current_bytes = len(header.encode("utf-8"))
        for card in values:
            card_bytes = len(str(card["text"]).encode("utf-8"))
            if (
                enabled
                and chunks[-1]
                and current_bytes + card_bytes > max_bytes
            ):
                chunks.append([])
                current_bytes = len(header.encode("utf-8"))
            chunks[-1].append(card)
            current_bytes += card_bytes
        stem = topic if not group else f"{topic}-{_slug(group)}"
        for index, chunk in enumerate(chunks, 1):
            filename = (
                f"{stem}.md"
                if len(chunks) == 1
                else f"{stem}-{index:06d}.md"
            )
            path = output / filename
            with path.open("w", encoding="utf-8") as handle:
                handle.write(header)
                for card in chunk:
                    handle.write(str(card["text"]))
            descriptors.append(
                {
                    "path": filename,
                    "topic": topic,
                    "source": group or None,
                    "sections": len(chunk),
                    "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
            )
            for card in chunk:
                reference = f"knowledge/{filename}#{card['anchor']}"
                for node_id in card["nodeIds"]:
                    references[str(node_id)].append(reference)
    return descriptors, references


def _api_heading(node: dict[str, str]) -> str:
    properties = _properties(node)
    method = str(properties.get("method") or "").upper()
    route = str(properties.get("route") or properties.get("path") or "")
    if method and route:
        return f"{method} {route}"
    return _display_name(node, node.get("node_id", ""))


def _api_narrative(
    graph: GraphPackage,
    node_id: str,
    callers: list[str],
    db_paths: list[tuple[str, list[str], list[str]]],
) -> str:
    node = graph.nodes[node_id]
    lines = [
        f"API `{node.get('qualified_name', node_id)}` is called by "
        f"{len(callers)} UI/client node(s) and reaches {len(db_paths)} database object(s)."
    ]
    lines.extend(_named_bullets(graph, "Callers", callers))
    lines.extend(
        _named_bullets(graph, "Database objects", [item[0] for item in db_paths])
    )
    return "\n\n".join(lines)


def _flow_narrative(
    graph: GraphPackage,
    api_paths: list[tuple[str, list[str], list[str]]],
    db_paths: list[tuple[str, list[str], list[str]]],
) -> str:
    lines = [
        f"This UI flow reaches {len(api_paths)} API operation(s) and "
        f"{len(db_paths)} database object(s)."
    ]
    lines.extend(
        _named_bullets(graph, "API operations", [item[0] for item in api_paths])
    )
    lines.extend(
        _named_bullets(graph, "Database objects", [item[0] for item in db_paths])
    )
    return "\n\n".join(lines)


def _database_narrative(
    graph: GraphPackage,
    readers: list[dict[str, str]],
    writers: list[dict[str, str]],
) -> str:
    lines = [
        f"This database object has {len(readers)} direct reader(s) and "
        f"{len(writers)} direct writer(s)."
    ]
    lines.extend(
        _named_bullets(
            graph, "Readers", [edge["source_node_id"] for edge in readers]
        )
    )
    lines.extend(
        _named_bullets(
            graph, "Writers", [edge["source_node_id"] for edge in writers]
        )
    )
    return "\n\n".join(lines)


def _job_narrative(
    graph: GraphPackage,
    db_paths: list[tuple[str, list[str], list[str]]],
) -> str:
    lines = [f"This job reaches {len(db_paths)} database object(s)."]
    lines.extend(
        _named_bullets(graph, "Database objects", [item[0] for item in db_paths])
    )
    return "\n\n".join(lines)


def _named_bullets(
    graph: GraphPackage, label: str, node_ids: Iterable[str]
) -> list[str]:
    values = [
        f"- `{_display_name(graph.nodes.get(node_id, {}), node_id)}` (`{node_id}`)"
        for node_id in sorted(set(node_ids))[:25]
    ]
    return [f"**{label}:**\n" + "\n".join(values)] if values else []


def _display_name(node: dict[str, str], fallback: str) -> str:
    return (
        node.get("default_display_name")
        or node.get("qualified_name")
        or node.get("technical_name")
        or fallback
    )


def _markdown_anchor(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"[^\w\s\-{}./]+", "", text, flags=re.UNICODE)
    return re.sub(r"[\s/]+", "-", text).strip("-") or "section"


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or "global"


def _coerce_positive_int(value: object, default: int, *, minimum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def _content_hash(
    record: dict[str, object], *, excluded: set[str] | None = None
) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in (excluded or set()) and key != "content_hash"
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _properties(row: dict[str, str]) -> dict[str, object]:
    try:
        value = json.loads(row.get("properties_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_graph_index(
    graph: GraphPackage,
    output: Path,
    *,
    generated_at: str,
    packages: dict[str, dict[str, object]],
    node_packages: dict[str, str],
    edge_packages: dict[str, str],
    evidence_packages: dict[str, str],
    issue_packages: dict[str, str],
    knowledge_refs: dict[str, list[str]],
    memory_refs: dict[str, dict[str, object]],
) -> None:
    node_counts: dict[str, int] = defaultdict(int)
    edge_counts: dict[str, int] = defaultdict(int)
    nodes_by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
    nodes_by_id: dict[str, dict[str, str]] = {}
    qualified_names: dict[str, list[dict[str, str]]] = defaultdict(list)
    apis: dict[str, list[dict[str, str]]] = defaultdict(list)
    tables: dict[str, list[dict[str, str]]] = defaultdict(list)
    for node_id, node in sorted(graph.nodes.items()):
        node_type = node.get("node_type", "UNKNOWN")
        package = node_packages.get(node_id, "global")
        locator = {"nodeId": node_id, "package": package}
        node_counts[node_type] += 1
        nodes_by_type[node_type].append(locator)
        nodes_by_id[node_id] = {"package": package}
        qualified = node.get("qualified_name", "")
        if qualified:
            qualified_names[qualified.casefold()].append(locator)
        if node_type == "API_OPERATION":
            signature = _api_index_key(node)
            if signature:
                apis[signature].append(locator)
        if _is_database_node(node):
            for table_key in _table_index_keys(node):
                tables[table_key].append(locator)

    edges_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    edges_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    edges_by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
    readers: dict[str, list[dict[str, str]]] = defaultdict(list)
    writers: dict[str, list[dict[str, str]]] = defaultdict(list)
    for edge_id, edge in sorted(graph.edges.items()):
        edge_type = edge.get("edge_type", "UNKNOWN")
        package = edge_packages.get(edge_id, "global")
        locator = {"edgeId": edge_id, "package": package}
        edge_counts[edge_type] += 1
        edges_by_source[edge.get("source_node_id", "")].append(locator)
        edges_by_target[edge.get("target_node_id", "")].append(locator)
        edges_by_type[edge_type].append(locator)
        if edge_type in _READ_EDGE_TYPES:
            readers[edge.get("target_node_id", "")].append(locator)
        if edge_type in _WRITE_EDGE_TYPES:
            writers[edge.get("target_node_id", "")].append(locator)

    evidence_by_target: dict[str, list[dict[str, str]]] = defaultdict(list)
    evidence_by_id: dict[str, dict[str, str]] = {}
    for evidence_id, evidence in sorted(graph.evidence.items()):
        locator = {
            "evidenceId": evidence_id,
            "package": evidence_packages.get(evidence_id, "global"),
        }
        evidence_by_target[evidence.get("target_id", "")].append(locator)
        evidence_by_id[evidence_id] = {
            "package": evidence_packages.get(evidence_id, "global")
        }

    source_by_path = {
        record.relative_path: record.source_key for record in graph.source_records
    }
    issues_by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    issues_by_id: dict[str, dict[str, str]] = {}
    for issue_id, issue in sorted(graph.issues.items()):
        package = issue_packages.get(issue_id, "global")
        issues_by_id[issue_id] = {"package": package}
        source = source_by_path.get(issue.get("source_path", ""))
        if not source and package.startswith("sources/"):
            source = package.split("/", 1)[1]
        source = source or "global"
        issues_by_source[source].append(
            {"issueId": issue_id, "package": package}
        )

    index_data = {
        "indexVersion": "2.0",
        "generatedAt": generated_at,
        "database": "graph.sqlite",
        "nodeTypeCounts": dict(sorted(node_counts.items())),
        "edgeTypeCounts": dict(sorted(edge_counts.items())),
        "packagesBySource": packages,
        "nodesByType": dict(sorted(nodes_by_type.items())),
        "nodesById": nodes_by_id,
        "qualifiedNames": dict(sorted(qualified_names.items())),
        "apisByMethodPath": dict(sorted(apis.items())),
        "tablesByQName": dict(sorted(tables.items())),
        "readersByTable": dict(sorted(readers.items())),
        "writersByTable": dict(sorted(writers.items())),
        "edgesBySource": dict(sorted(edges_by_source.items())),
        "edgesByTarget": dict(sorted(edges_by_target.items())),
        "edgesByType": dict(sorted(edges_by_type.items())),
        "knowledgeByNodeId": knowledge_refs,
        "memoryByNodeId": memory_refs,
        "evidenceByTargetId": dict(sorted(evidence_by_target.items())),
        "evidenceById": evidence_by_id,
        "issuesBySource": dict(sorted(issues_by_source.items())),
        "issuesById": issues_by_id,
    }
    (output / "graph-index.json").write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _api_index_key(node: dict[str, str]) -> str:
    properties = _properties(node)
    method = str(properties.get("method") or "").upper()
    route = str(properties.get("route") or properties.get("path") or "")
    if method and route:
        try:
            from .contract.graph_contract import normalize_http_route

            normalized_method, normalized_route = normalize_http_route(method, route)
            return f"{normalized_method} {normalized_route}"
        except ValueError:
            pass
    qualified = node.get("qualified_name", "")
    first, separator, rest = qualified.partition(" ")
    return f"{first.upper()} {rest}" if separator and first.isalpha() else ""


def _table_index_keys(node: dict[str, str]) -> set[str]:
    properties = _properties(node)
    database = str(
        node.get("database_key")
        or properties.get("database")
        or properties.get("database_key")
        or ""
    ).upper()
    schema = str(
        properties.get("schema") or properties.get("owner") or ""
    ).upper()
    name = str(
        properties.get("table")
        or properties.get("object_name")
        or node.get("technical_name")
        or ""
    ).upper()
    if "." in name:
        parts = [part.strip('"') for part in name.split(".") if part]
        if len(parts) >= 2 and not schema:
            schema = parts[-2].upper()
        name = parts[-1].upper()
    values = {
        str(node.get("qualified_name") or "").upper(),
        name,
        ".".join(part for part in (database, name) if part),
        ".".join(part for part in (database, schema, name) if part),
    }
    return {value for value in values if value}


def _is_database_node(node: dict[str, str]) -> bool:
    if node.get("node_type") in _DB_NODE_TYPES:
        return True
    return (
        node.get("node_type") == "UNRESOLVED_REFERENCE"
        and bool(_properties(node).get("table"))
    )


def _matches_target_type(
    node: dict[str, str], target_types: frozenset[str]
) -> bool:
    return node.get("node_type") in target_types or (
        target_types == _DB_NODE_TYPES and _is_database_node(node)
    )


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
    return json.dumps(
        value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


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
        raise ValueError(
            f"Refusing to replace unmanaged output directory: {path}"
        ) from exc
    extractor = manifest.get("extractor") if isinstance(manifest, dict) else None
    metadata = manifest.get("metadata") if isinstance(manifest, dict) else None
    managed = (
        isinstance(extractor, dict) and extractor.get("name") == "code-tree-exporter"
    ) or (
        isinstance(metadata, dict) and metadata.get("managedBy") == "code-tree-exporter"
    )
    if not managed:
        raise ValueError(f"Refusing to replace unmanaged output directory: {path}")


def _replace_ref_node_id(value: object, old_target: str, new_target: str) -> bool:
    changed = False
    if isinstance(value, dict):
        if value.get("ref_node_id") == old_target:
            value["ref_node_id"] = new_target
            value["resolution"] = "resolved"
            changed = True
        ref_node_ids = value.get("ref_node_ids")
        if isinstance(ref_node_ids, list) and old_target in ref_node_ids:
            value["ref_node_ids"] = [
                new_target if item == old_target else item for item in ref_node_ids
            ]
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
