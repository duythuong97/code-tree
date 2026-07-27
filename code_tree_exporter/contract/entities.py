"""Language-agnostic dataclasses for graph extraction results.

Every extractor returns ExtractionResult containing lists of GraphNode and
GraphEdge.  These are plain data — no DB coupling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .graph_contract import normalize_repository_path


@dataclass
class GraphNode:
    """A node to be MERGE'd into Neo4j.

    label       : Neo4j label, e.g. "Table", "Function", "ApiEndpoint"
    key         : property used as unique identifier within this label
    key_value   : value of that property
    properties  : additional properties to set/update on MERGE
    source      : "extracted" | "manual" | "rule"
    """
    label: str
    key: str
    key_value: str
    properties: dict[str, Any] = field(default_factory=dict)
    source: str = "extracted"

    def identity(self) -> tuple[str, str, str]:
        return (self.label, self.key, self.key_value)


@dataclass
class GraphEdge:
    """A relationship to be MERGE'd into Neo4j.

    from_label / from_key / from_key_value  : identifies the source node
    to_label   / to_key   / to_key_value    : identifies the target node
    rel_type    : Neo4j relationship type, e.g. "WRITES_TO", "READS_FROM"
    properties  : additional properties on the relationship
    """
    from_label: str
    from_key: str
    from_key_value: str
    to_label: str
    to_key: str
    to_key_value: str
    rel_type: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    """Output of a single extractor run on one file."""
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    source_file: str = ""
    extractor_name: str = ""

    def merge(self, other: "ExtractionResult") -> "ExtractionResult":
        return ExtractionResult(
            nodes=self.nodes + other.nodes,
            edges=self.edges + other.edges,
            source_file=self.source_file,
            extractor_name=f"{self.extractor_name}+{other.extractor_name}",
        )

    def is_empty(self) -> bool:
        return not self.nodes and not self.edges


@dataclass(frozen=True)
class MetadataFact:
    entity_kind: str
    entity_id: str
    payload: dict[str, Any]
    row_order: int

@dataclass
class ExtractionContext:
    """Shared context passed to every extractor for a given file.

    Provides repository metadata and configurable naming rules so extractors
    can infer service / domain names without hardcoding.
    """
    repository: str = ""
    repository_path: str = ""
    domain: str = ""
    service_name: str = ""
    # C#: strip this prefix from namespace to derive service name
    # e.g. "Company.Orders" → service "orders"
    namespace_prefix: str = ""
    # Extra key-value tags propagated to every node created in this context
    extra_tags: dict[str, Any] = field(default_factory=dict)

    # VCS / repository metadata
    source: str = "git"       # "git" | "svn"
    vcs_url: str = ""
    repo_owner: str = ""
    team_name: str = ""

    # Stable extraction and database identity.
    db_name: str = ""
    schema_name: str = ""
    source_id: str = ""
    relative_source_path: str = ""
    project_name: str = ""
    project_relative_root: str = ""
    project_file: str = ""
    owner_policy: str = ""

    # Workflow / scheduling metadata (JP1, Airflow, cron, Hangfire …)
    workflow_name: str = ""   # JP1 Jobnet name / Airflow DAG ID
    workflow_id: str = ""     # External scheduler ID
    scheduler_type: str = ""  # "jp1" | "airflow" | "cron" | "hangfire"

    def repo_qname(self) -> str:
        return f"Repository:{self.repository}:{self.project_name or self.repository}"

    def repository_owner_qname(self, namespace: str, class_name: str) -> str:
        owner = ".".join(part for part in (namespace, class_name) if part)
        return f"Repository:{self.repository}:{self.project_name or self.repository}:{owner}"

    def application_qname(self) -> str:
        return f"Application:{self.repository}:{self.project_name or self.repository}"

    def source_file_qname(self) -> str:
        if not self.source_id or not self.relative_source_path:
            raise ValueError("SourceFile identity requires source_id and relative_source_path")
        return f"SourceFile:{self.source_id}:{normalize_repository_path(self.relative_source_path)}"

    def resolved_object(
        self, object_name: str, explicit_schema: str | None = None
    ) -> tuple[str, str, bool]:
        normalized = _normalize_name(object_name)
        if "." in normalized:
            schema, name = normalized.rsplit(".", 1)
            return schema, name, False
        schema = _normalize_name(explicit_schema or self.schema_name)
        if schema:
            return schema, normalized, False
        unresolved_schema = f"UNRESOLVED[{self.source_id or self.repository or 'UNKNOWN'}]"
        return unresolved_schema, normalized, True

    def table_qname(self, table_name: str, explicit_schema: str | None = None) -> str:
        schema, name, _ = self.resolved_object(table_name, explicit_schema)
        return f"Table:{self.db_name}:{schema}.{name}"

    def sequence_qname(
        self, sequence_name: str, explicit_schema: str | None = None
    ) -> str:
        schema, name, _ = self.resolved_object(sequence_name, explicit_schema)
        return f"Sequence:{self.db_name}:{schema}.{name}"

    def column_qname(
        self, table_name: str, column_name: str, explicit_schema: str | None = None
    ) -> str:
        table_identity = self.table_qname(table_name, explicit_schema).removeprefix("Table:")
        return f"Column:{table_identity}:{_normalize_name(column_name)}"

    def logic_qname(
        self, label: str, name: str, explicit_schema: str | None = None
    ) -> str:
        schema = _normalize_name(explicit_schema or self.schema_name)
        if not schema:
            schema = f"UNRESOLVED[{self.source_id or self.repository or 'UNKNOWN'}]"
        return f"{label}:{self.repository}:{self.db_name}:{schema}:{_normalize_name(name)}"

    def evidence_properties(self, line: int | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source_id": self.source_id,
            "source_path": normalize_repository_path(self.relative_source_path),
        }
        if line is not None:
            out["line"] = line
        return out

    def infer_service_from_namespace(self, namespace: str) -> str:
        """Strip namespace_prefix and return the next segment as service name."""
        if self.service_name:
            return self.service_name
        if self.namespace_prefix and namespace.startswith(self.namespace_prefix):
            remainder = namespace[len(self.namespace_prefix):].lstrip(".")
            return remainder.split(".")[0].lower() if remainder else self.repository
        return self.repository

    def infer_service_from_path(self, file_path: str) -> str:
        """Derive service name from folder structure when namespace is unavailable."""
        if self.service_name:
            return self.service_name
        parts = PurePosixPath(normalize_repository_path(file_path)).parts
        # Use repository root folder name or first meaningful segment
        for part in parts:
            if part not in {"src", "lib", "app", "main", "java", "cs", "ts", ".", ".."}:
                return part.lower()
        return self.repository


def _normalize_name(value: str) -> str:
    parts = []
    for raw in str(value or "").split("."):
        part = raw.strip()
        parts.append(
            part[1:-1]
            if part.startswith('"') and part.endswith('"')
            else part.upper()
        )
    return ".".join(filter(None, parts))
