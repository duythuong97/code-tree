from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from .contract.graph_contract import canonical_edge_id, normalize_http_route, stable_node_id
from .graph_package import GraphPackage

_API_CALL_TYPES = frozenset({"API_CALL_REFERENCE", "API_CLIENT_CALL"})
_DB_OBJECT_TYPES = frozenset({"TABLE", "VIEW", "MATERIALIZED_VIEW"})
_ROUTINE_TYPES = frozenset({"PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"})
_DEFERRED_REFERENCE_TYPES = frozenset(
    {"UNRESOLVED_REFERENCE", "EXTERNAL_DATABASE_OBJECT"}
)
_DB_EDGE_TYPES = frozenset(
    {
        "READS",
        "REMOTE_READS",
        "INSERTS",
        "UPDATES",
        "DELETES",
        "MERGES",
        "WRITES",
        "READS_FROM",
        "WRITES_TO",
        "LOADS_INTO",
        "MAPS_TO",
    }
)
_STALE_REFERENCE_ISSUES = frozenset(
    {
        "TABLE_NOT_IMPORTED",
        "COLUMN_NOT_IMPORTED",
        "PROCEDURE_NOT_FOUND",
        "EXTERNAL_OBJECT",
    }
)


def run_linker(graph: GraphPackage) -> None:
    """Resolve cross-source references after every source package is merged."""
    link_api(graph)
    resolve_deferred_references(graph)
    materialize_dangling_references(graph)


def link_api(graph: GraphPackage) -> None:
    """Link frontend API calls using exact and pre-indexed suffix matches."""
    operations: dict[tuple[str, str], list[str]] = defaultdict(list)
    routes: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    suffixes: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    for node_id, node in graph.nodes.items():
        if node.get("node_type") != "API_OPERATION":
            continue
        signature = _api_signature(node)
        if signature is None:
            continue
        operations[signature].append(node_id)
        method, route = signature
        parts = _route_parts(route)
        routes[(method, parts)].add(node_id)
        for index in range(len(parts)):
            suffixes[(method, parts[index:])].add(node_id)

    for node_id, node in list(graph.nodes.items()):
        if node.get("node_type") not in _API_CALL_TYPES:
            continue
        signature = _api_signature(node)
        if signature is None:
            continue
        candidates = list(operations.get(signature, ()))
        match_kind = "exact"
        if not candidates:
            candidates = sorted(_api_suffix_candidates(signature, routes, suffixes))
            match_kind = "unique_suffix"
        if len(candidates) == 1:
            _put_edge(
                graph,
                node_id,
                "CALLS_API",
                candidates[0],
                confidence="1.0" if match_kind == "exact" else "0.8",
                properties={"match": match_kind},
            )
        elif len(candidates) > 1:
            graph.add_issue(
                "AMBIGUOUS_API_LINK",
                f"API call {signature[0]} {signature[1]} matches {len(candidates)} operations",
                severity="WARNING",
                properties={
                    "source_node_id": node_id,
                    "candidate_node_ids": sorted(candidates),
                },
            )


def link_db(graph: GraphPackage) -> None:
    """Compatibility entry point for resolving all deferred database references."""
    resolve_deferred_references(graph, include_routines=False)


def link_routines(graph: GraphPackage) -> None:
    """Compatibility entry point for resolving deferred routine references."""
    resolve_deferred_references(graph, include_database=False)


def resolve_deferred_references(
    graph: GraphPackage,
    *,
    include_database: bool = True,
    include_routines: bool = True,
) -> None:
    indexes = _resolution_indexes(graph)
    edge_types_by_target: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges.values():
        edge_types_by_target[edge.get("target_node_id", "")].add(
            edge.get("edge_type", "")
        )
    replacements: dict[str, str] = {}
    for node_id, node in graph.nodes.items():
        if node.get("node_type") not in _DEFERRED_REFERENCE_TYPES:
            continue
        kind, candidates = _reference_candidates(
            node,
            indexes,
            include_database=include_database,
            include_routines=include_routines,
            database_reference=bool(
                edge_types_by_target.get(node_id, set()) & _DB_EDGE_TYPES
            ),
        )
        candidates.discard(node_id)
        if len(candidates) == 1:
            replacements[node_id] = next(iter(candidates))
        elif len(candidates) > 1:
            issue_type = "AMBIGUOUS_SYMBOL" if kind == "routine" else "AMBIGUOUS_DB_LINK"
            graph.add_issue(
                issue_type,
                f"Deferred {kind or 'database'} reference matches {len(candidates)} nodes",
                severity="WARNING",
                properties={
                    "source_node_id": node_id,
                    "candidate_node_ids": sorted(candidates),
                },
            )
    if replacements:
        _apply_replacements(graph, replacements)
    _prune_resolved_reference_issues(
        graph,
        indexes,
        include_database=include_database,
        include_routines=include_routines,
    )


def materialize_dangling_references(graph: GraphPackage) -> None:
    """Convert still-missing edge endpoints into explicit unresolved nodes."""
    missing: dict[str, str] = {}

    def remember(node_id: str) -> None:
        if node_id and node_id not in graph.nodes:
            missing.setdefault(node_id, _deferred_placeholder_id(node_id))

    for edge in graph.edges.values():
        for field in ("source_node_id", "target_node_id"):
            remember(edge.get(field, ""))
    for evidence in graph.evidence.values():
        if evidence.get("target_type") == "NODE":
            remember(evidence.get("target_id", ""))
    for comment in graph.comments.values():
        remember(comment.get("owner_node_id", ""))
    for issue in graph.issues.values():
        remember(issue.get("source_node_id", ""))
    for collection in (
        graph.nodes,
        graph.edges,
        graph.evidence,
        graph.comments,
        graph.issues,
    ):
        for row in collection.values():
            for node_id in _property_node_references(
                row.get("properties_json", "{}")
            ):
                remember(node_id)

    orphan_evidence = [
        evidence_id
        for evidence_id, evidence in graph.evidence.items()
        if evidence.get("target_type") == "EDGE"
        and evidence.get("target_id") not in graph.edges
    ]
    for evidence_id in orphan_evidence:
        evidence = graph.evidence.pop(evidence_id)
        graph.add_issue(
            "UNRESOLVED_REFERENCE",
            "Evidence referenced an edge that was not present after global merge",
            severity="WARNING",
            source_path=evidence.get("source_path", ""),
            properties={"target_type": "EDGE"},
        )
    if not missing:
        return

    context_by_missing: dict[str, dict[str, str]] = {}
    for edge in graph.edges.values():
        source_id = edge.get("source_node_id", "")
        target_id = edge.get("target_node_id", "")
        if source_id in missing and target_id in graph.nodes:
            context_by_missing.setdefault(source_id, graph.nodes[target_id])
        if target_id in missing and source_id in graph.nodes:
            context_by_missing.setdefault(target_id, graph.nodes[source_id])

    for expected_id, placeholder_id in missing.items():
        context = context_by_missing.get(expected_id, {})
        identity = _identity_from_stable_id(expected_id)
        label = identity.get("name") or "unresolved reference"
        graph.nodes[placeholder_id] = {
            "node_id": placeholder_id,
            "node_type": "UNRESOLVED_REFERENCE",
            "technical_name": label,
            "qualified_name": identity.get("qualified_name") or label,
            "default_display_name": label,
            "system_key": context.get("system_key", ""),
            "database_key": identity.get("database") or context.get("database_key", ""),
            "repository_key": context.get("repository_key", ""),
            "graph_role": "TECHNICAL",
            "confidence": "0.1",
            "properties_json": json.dumps(
                {
                    **identity,
                    "resolution": "missing_after_global_merge",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
        graph.add_issue(
            "UNRESOLVED_REFERENCE",
            f"Referenced {identity.get('kind') or 'node'} {label} "
            "was not present after global merge",
            severity="WARNING",
            properties={
                "reference_kind": identity.get("kind", "node"),
                "database": identity.get("database", ""),
                "name": label,
            },
        )
    _apply_replacements(graph, missing)


def _resolution_indexes(graph: GraphPackage) -> dict[str, dict[tuple[str, ...], set[str]]]:
    objects: dict[tuple[str, ...], set[str]] = defaultdict(set)
    columns: dict[tuple[str, ...], set[str]] = defaultdict(set)
    routines: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for node_id, node in graph.nodes.items():
        node_type = node.get("node_type", "")
        if node_type in _DB_OBJECT_TYPES:
            database, schema, name = _db_object_identity(node)
            if database and name:
                objects[(database, schema, name)].add(node_id)
                objects[(database, "", name)].add(node_id)
        elif node_type == "COLUMN":
            database, schema, table, column = _column_identity(node)
            if database and table and column:
                columns[(database, schema, table, column)].add(node_id)
                columns[(database, "", table, column)].add(node_id)
        elif node_type in _ROUTINE_TYPES:
            database, package, routine = _routine_identity(node_id, node)
            if database and routine:
                routines[(database, package, routine)].add(node_id)
                routines[(database, "", routine)].add(node_id)
    return {"objects": objects, "columns": columns, "routines": routines}


def _reference_candidates(
    node: dict[str, str],
    indexes: dict[str, dict[tuple[str, ...], set[str]]],
    *,
    include_database: bool,
    include_routines: bool,
    database_reference: bool,
) -> tuple[str, set[str]]:
    properties = _properties(node)
    if "@" in str(properties.get("raw_reference") or node.get("technical_name") or ""):
        return "remote_object", set()
    if properties.get("routine"):
        if not include_routines:
            return "routine", set()
        database = _upper(node.get("database_key") or properties.get("database"))
        package = _upper(properties.get("package"))
        routine = _upper(properties.get("routine"))
        candidates = set(indexes["routines"].get((database, package, routine), ()))
        if not candidates and package:
            candidates.update(indexes["routines"].get((database, "", routine), ()))
        return "routine", candidates
    if properties.get("column"):
        if not include_database:
            return "column", set()
        database = _upper(node.get("database_key") or properties.get("database"))
        schema = _upper(properties.get("schema") or properties.get("owner"))
        table = _upper(properties.get("table"))
        column = _upper(properties.get("column"))
        candidates = set(
            indexes["columns"].get((database, schema, table, column), ())
        )
        if not candidates and schema:
            candidates.update(
                indexes["columns"].get((database, "", table, column), ())
            )
        return "column", candidates
    if include_database and (
        database_reference
        or node.get("node_type") == "EXTERNAL_DATABASE_OBJECT"
        or any(
            properties.get(key) for key in ("table", "object", "object_name")
        )
    ):
        database, schema, name = _db_object_identity(node)
        candidates = set(indexes["objects"].get((database, schema, name), ()))
        if not candidates and schema:
            candidates.update(indexes["objects"].get((database, "", name), ()))
        return "object", candidates
    return "", set()


def _apply_replacements(graph: GraphPackage, replacements: dict[str, str]) -> None:
    edge_aliases: dict[str, str] = {}
    edges: dict[str, dict[str, str]] = {}
    for old_edge_id, original in graph.edges.items():
        edge = dict(original)
        edge["source_node_id"] = replacements.get(
            edge.get("source_node_id", ""), edge.get("source_node_id", "")
        )
        edge["target_node_id"] = replacements.get(
            edge.get("target_node_id", ""), edge.get("target_node_id", "")
        )
        new_edge_id = canonical_edge_id(
            edge["source_node_id"],
            edge["edge_type"],
            edge["target_node_id"],
            edge.get("raw_operation", ""),
            edge["graph_layer"],
        )
        edge["edge_id"] = new_edge_id
        if new_edge_id in edges:
            _merge_edge_rows(edges[new_edge_id], edge)
        else:
            edges[new_edge_id] = edge
        edge_aliases[old_edge_id] = new_edge_id
    graph.edges = edges

    aliases = {**replacements, **edge_aliases}
    for evidence in graph.evidence.values():
        target_id = evidence.get("target_id", "")
        target_aliases = replacements if evidence.get("target_type") == "NODE" else edge_aliases
        evidence["target_id"] = target_aliases.get(target_id, target_id)
    for comment in graph.comments.values():
        owner_id = comment.get("owner_node_id", "")
        comment["owner_node_id"] = replacements.get(owner_id, owner_id)
    for issue in graph.issues.values():
        source_id = issue.get("source_node_id", "")
        issue["source_node_id"] = replacements.get(source_id, source_id)
    for collection in (
        graph.nodes,
        graph.edges,
        graph.evidence,
        graph.comments,
        graph.issues,
    ):
        for row in collection.values():
            row["properties_json"] = _rewrite_properties(
                row.get("properties_json", "{}"), aliases
            )
    for old_id in replacements:
        graph.nodes.pop(old_id, None)


def _prune_resolved_reference_issues(
    graph: GraphPackage,
    indexes: dict[str, dict[tuple[str, ...], set[str]]],
    *,
    include_database: bool,
    include_routines: bool,
) -> None:
    retained: dict[str, dict[str, str]] = {}
    for issue_id, issue in graph.issues.items():
        if issue.get("issue_type") not in _STALE_REFERENCE_ISSUES:
            retained[issue_id] = issue
            continue
        if issue.get("issue_type") == "PROCEDURE_NOT_FOUND":
            if not include_routines:
                retained[issue_id] = issue
                continue
        elif not include_database:
            retained[issue_id] = issue
            continue
        database = _upper(issue.get("database_key"))
        raw = str(issue.get("raw_reference") or "").strip().upper()
        if not database or not raw or "@" in raw:
            retained[issue_id] = issue
            continue
        parts = [part.strip('"') for part in raw.split(".") if part]
        resolved = False
        if issue.get("issue_type") == "COLUMN_NOT_IMPORTED" and len(parts) >= 2:
            table, column = parts[-2:]
            resolved = bool(indexes["columns"].get((database, "", table, column)))
        elif issue.get("issue_type") == "PROCEDURE_NOT_FOUND":
            package = parts[-2] if len(parts) >= 2 else ""
            routine = parts[-1]
            resolved = bool(
                indexes["routines"].get((database, package, routine))
                or indexes["routines"].get((database, "", routine))
            )
        else:
            schema = parts[-2] if len(parts) >= 2 else ""
            name = parts[-1]
            resolved = bool(
                indexes["objects"].get((database, schema, name))
                or indexes["objects"].get((database, "", name))
            )
        if not resolved:
            retained[issue_id] = issue
    graph.issues = retained


def _api_signature(node: dict[str, str]) -> tuple[str, str] | None:
    properties = _properties(node)
    method = str(properties.get("method") or "").strip()
    route = str(properties.get("route") or properties.get("path") or "").strip()
    if not method or not route:
        qualified = str(node.get("qualified_name") or node.get("technical_name") or "")
        head, separator, tail = qualified.strip().partition(" ")
        if separator and head.isalpha():
            method = method or head
            route = route or tail
    if not method or not route:
        return None
    try:
        return normalize_http_route(method, route)
    except ValueError:
        return None


def _api_suffix_candidates(
    signature: tuple[str, str],
    routes: dict[tuple[str, tuple[str, ...]], set[str]],
    suffixes: dict[tuple[str, tuple[str, ...]], set[str]],
) -> set[str]:
    method, route = signature
    parts = _route_parts(route)
    matches = set(suffixes.get((method, parts), ()))
    for index in range(len(parts)):
        matches.update(routes.get((method, parts[index:]), ()))
    return matches


def _route_parts(route: str) -> tuple[str, ...]:
    return tuple(part for part in route.split("/") if part)


def _db_object_identity(node: dict[str, str]) -> tuple[str, str, str]:
    properties = _properties(node)
    database = _upper(
        node.get("database_key")
        or properties.get("database")
        or properties.get("database_key")
    )
    schema = _upper(properties.get("schema") or properties.get("owner"))
    name = _upper(
        properties.get("table")
        or properties.get("object_name")
        or properties.get("object")
        or node.get("technical_name")
    )
    if "." in name:
        parts = [part.strip('"') for part in name.split(".") if part]
        if len(parts) >= 2 and not schema:
            schema = parts[-2].upper()
        name = parts[-1].upper()
    return database, schema, name


def _column_identity(node: dict[str, str]) -> tuple[str, str, str, str]:
    properties = _properties(node)
    database = _upper(node.get("database_key") or properties.get("database"))
    schema = _upper(properties.get("schema") or properties.get("owner"))
    table = _upper(properties.get("table"))
    column = _upper(properties.get("column") or node.get("technical_name"))
    if not table:
        parts = [
            part.strip('"')
            for part in str(node.get("qualified_name") or "").split(".")
            if part
        ]
        if len(parts) >= 2:
            table, column = parts[-2].upper(), parts[-1].upper()
        if len(parts) >= 3 and not schema:
            schema = parts[-3].upper()
    return database, schema, table, column


def _routine_identity(node_id: str, node: dict[str, str]) -> tuple[str, str, str]:
    properties = _properties(node)
    database = _upper(node.get("database_key") or properties.get("database"))
    package = _upper(properties.get("package") or properties.get("owner"))
    routine = _upper(properties.get("routine") or node.get("technical_name"))
    parts = node_id.split(":")
    if not package and len(parts) >= 4:
        package = parts[2].upper()
    return database, package, routine


def _deferred_placeholder_id(expected_id: str) -> str:
    digest = hashlib.sha256(expected_id.encode("utf-8")).hexdigest()[:24]
    return stable_node_id("unresolved-reference", "DEFERRED", digest)


def _identity_from_stable_id(stable_id: str) -> dict[str, str]:
    parts = stable_id.split(":")
    prefix = parts[0] if parts else ""
    kind = prefix.replace("-", "_").upper() if prefix else "NODE"
    database = parts[1].upper() if len(parts) >= 3 and prefix in {
        "table",
        "column",
        "procedure",
        "function",
        "sequence",
        "view",
        "materialized-view",
    } else ""
    name = parts[-1] if len(parts) > 1 else stable_id
    if prefix in {"procedure", "function", "local-routine"} and len(parts) >= 2:
        name = parts[-2]
    identity = {
        "kind": kind,
        "database": database,
        "name": name,
        "qualified_name": ".".join(part for part in parts[1:] if part),
    }
    if prefix in {"table", "view", "materialized-view"}:
        identity["table"] = name
    elif prefix == "column" and len(parts) >= 4:
        identity["table"] = parts[-2]
        identity["column"] = parts[-1]
    elif prefix in {"procedure", "function", "local-routine"} and len(parts) >= 5:
        identity["package"] = parts[2]
        identity["routine"] = parts[-2]
    return identity


def _property_node_references(value: str) -> set[str]:
    try:
        properties = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return set()
    result: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, dict):
            ref = item.get("ref_node_id")
            if isinstance(ref, str) and ref:
                result.add(ref)
            refs = item.get("ref_node_ids")
            if isinstance(refs, list):
                result.update(value for value in refs if isinstance(value, str) and value)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(properties)
    return result


def _put_edge(
    graph: GraphPackage,
    source: str,
    edge_type: str,
    target: str,
    *,
    confidence: str,
    properties: dict[str, object],
) -> None:
    edge_id = canonical_edge_id(source, edge_type, target, "", "DATA_FLOW")
    graph.edges.setdefault(
        edge_id,
        {
            "edge_id": edge_id,
            "source_node_id": source,
            "target_node_id": target,
            "edge_type": edge_type,
            "graph_layer": "DATA_FLOW",
            "raw_operation": "",
            "confidence": confidence,
            "properties_json": json.dumps(
                properties,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )


def _merge_edge_rows(target: dict[str, str], candidate: dict[str, str]) -> None:
    try:
        if float(candidate.get("confidence", "0")) > float(
            target.get("confidence", "0")
        ):
            target["confidence"] = candidate["confidence"]
    except ValueError:
        pass
    left = _properties(target)
    right = _properties(candidate)
    for key, value in right.items():
        left.setdefault(key, value)
    target["properties_json"] = json.dumps(
        left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _rewrite_properties(value: str, aliases: dict[str, str]) -> str:
    try:
        properties = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return value

    def rewrite(item: object) -> object:
        if isinstance(item, str):
            return aliases.get(item, item)
        if isinstance(item, list):
            return [rewrite(value) for value in item]
        if isinstance(item, dict):
            return {key: rewrite(value) for key, value in item.items()}
        return item

    return json.dumps(
        rewrite(properties),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _properties(row: dict[str, str]) -> dict[str, object]:
    try:
        value = json.loads(row.get("properties_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _upper(value: object) -> str:
    return str(value or "").strip().upper()
