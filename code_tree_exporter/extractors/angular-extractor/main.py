#!/usr/bin/env python3
"""Safe degraded Angular fallback used only when the TypeScript runtime fails.

This scanner deliberately emits declarations and literal facts only. It does
not attempt symbol resolution or assign confidence above 0.5 to inferred links.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.contract.graph_contract import (
    normalize_http_route,
    stable_node_id,
)
from code_tree_exporter.extractors.package_support.package_writer import (
    PackageBuilder,
    api_call_id,
    configured_files,
    line_for_offset,
    line_text,
)

_VERSION = "3.0.0-fallback"
_CLASS_RE = re.compile(
    r"@(?P<decorator>Component|Injectable)\s*\((?P<meta>.*?)\)\s*"
    r"(?:export\s+)?class\s+(?P<name>[A-Za-z_]\w*)",
    re.DOTALL,
)
_PLAIN_CLASS_RE = re.compile(r"\bexport\s+class\s+([A-Za-z_]\w*)")
_METHOD_RE = re.compile(
    r"(?m)^\s*(?:public|private|protected|static|async|\s)*"
    r"(?P<name>[A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?::[^={]+)?\{"
)
_ROUTE_RE = re.compile(
    r"\bpath\s*:\s*['\"](?P<path>[^'\"]*)['\"]"
    r"(?:(?!\}\s*,).)*?\bcomponent\s*:\s*(?P<component>[A-Za-z_]\w*)",
    re.DOTALL,
)
_HTTP_RE = re.compile(
    r"(?:this\.)?(?:http|httpClient)\."
    r"(?P<method>get|post|put|delete|patch|request)\s*"
    r"\(\s*(?P<argument>[^,\n;)]+)",
    re.IGNORECASE,
)
_EVENT_RE = re.compile(
    r"\((?P<event>click|submit|change)\)\s*=\s*"
    r"['\"](?P<handler>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_ROUTER_LINK_RE = re.compile(
    r"(?:routerLink|\[routerLink\])\s*=\s*['\"](?P<route>[^'\"]+)['\"]",
    re.IGNORECASE,
)
_SELECTOR_RE = re.compile(r"\bselector\s*:\s*['\"]([^'\"]+)['\"]")
_TEMPLATE_URL_RE = re.compile(r"\btemplateUrl\s*:\s*['\"]([^'\"]+)['\"]")
_QUOTED_RE = re.compile(r"^(['\"])(.*)\1$", re.DOTALL)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the degraded Angular literal/declaration fallback."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    extract(_load_config(Path(args.config)))
    return 0


def extract(config: dict) -> None:
    if config.get("type") != "angular":
        raise ValueError("Config type must be angular")
    source = str(config["source"])
    repository = str(config.get("repository") or source)
    system_key = str(config.get("system") or source)
    files = configured_files(config, [".ts", ".html"])
    builder = PackageBuilder(
        f"angular-fallback-{source}",
        f"extractor:angular-fallback/{source}",
        "angular-fallback",
        _VERSION,
        {
            "source": source,
            "technology": "safe literal/declaration fallback",
            "degraded": True,
            "extractorContract": "3.0",
            "capabilities": ["route-input-output", "api-client-lineage"],
        },
    )
    builder.files_scanned = len(files)
    project_id = stable_node_id("angular-project", repository, source)
    builder.add_node(
        project_id,
        "ANGULAR_PROJECT",
        source,
        source,
        source,
        system_key=system_key,
        repository_key=repository,
        graph_role="TECHNICAL",
        confidence=0.5,
        properties={"degraded": True},
    )
    builder.add_issue(
        "SEMANTIC_TREE_UNAVAILABLE",
        "WARNING",
        "TypeScript semantic parser unavailable; safe Angular fallback used",
        source_node_id=project_id,
        source_path=files[0].relative if files else "",
        start_line=1 if files else None,
        properties={
            "fallback": "angular-fallback",
            "confidence_ceiling": 0.5,
        },
    )

    classes: dict[str, str] = {}
    methods_by_file: dict[str, list[tuple[int, str]]] = {}
    for file in files:
        if file.absolute.suffix.lower() != ".ts":
            continue
        file_methods = []
        decorated_ranges = []
        for match in _CLASS_RE.finditer(file.text):
            decorator = match.group("decorator")
            name = match.group("name")
            node_type = (
                "ANGULAR_COMPONENT"
                if decorator == "Component"
                else "ANGULAR_SERVICE"
            )
            prefix = "angular-component" if node_type == "ANGULAR_COMPONENT" else "angular-service"
            node_id = stable_node_id(prefix, source, name)
            classes[name] = node_id
            decorated_ranges.append((match.start(), name, node_id))
            metadata = match.group("meta")
            properties = {"fallback": True}
            selector = _first_group(_SELECTOR_RE, metadata)
            template_url = _first_group(_TEMPLATE_URL_RE, metadata)
            if selector:
                properties["selector"] = selector
            if template_url:
                properties["templateUrl"] = template_url
            builder.add_node(
                node_id,
                node_type,
                name,
                f"{source}.{name}",
                name,
                system_key=system_key,
                repository_key=repository,
                graph_role="TECHNICAL",
                confidence=0.5,
                properties=properties,
            )
            builder.add_edge(
                project_id,
                node_id,
                "CONTAINS",
                graph_layer="STRUCTURAL",
                confidence=0.5,
            )
            line = line_for_offset(file.text, match.start())
            builder.add_evidence(
                "NODE",
                node_id,
                file.relative,
                line,
                line,
                "DECLARATION",
                line_text(file.text, line),
                confidence=0.5,
            )

        for class_match in _PLAIN_CLASS_RE.finditer(file.text):
            name = class_match.group(1)
            if name in classes:
                continue
            node_id = stable_node_id("class", source, name)
            classes[name] = node_id
            decorated_ranges.append((class_match.start(), name, node_id))
            builder.add_node(
                node_id,
                "CLASS",
                name,
                f"{source}.{name}",
                name,
                system_key=system_key,
                repository_key=repository,
                graph_role="TECHNICAL",
                confidence=0.5,
                properties={"fallback": True},
            )
            line = line_for_offset(file.text, class_match.start())
            builder.add_evidence(
                "NODE",
                node_id,
                file.relative,
                line,
                line,
                "DECLARATION",
                line_text(file.text, line),
                confidence=0.5,
            )

        decorated_ranges.sort()
        for method in _METHOD_RE.finditer(file.text):
            name = method.group("name")
            owner = _nearest_owner(decorated_ranges, method.start())
            if not owner or name == "constructor":
                continue
            method_id = stable_node_id(
                "method", source, owner[1], name, file.relative
            )
            file_methods.append((method.start(), method_id))
            builder.add_node(
                method_id,
                "METHOD",
                name,
                f"{source}.{owner[1]}.{name}",
                name,
                system_key=system_key,
                repository_key=repository,
                graph_role="TECHNICAL",
                confidence=0.5,
                properties={"fallback": True, "owner_node_id": owner[2]},
            )
            builder.add_edge(
                owner[2],
                method_id,
                "CONTAINS",
                graph_layer="STRUCTURAL",
                confidence=0.5,
            )
            line = line_for_offset(file.text, method.start())
            builder.add_evidence(
                "NODE",
                method_id,
                file.relative,
                line,
                line,
                "DECLARATION",
                line_text(file.text, line),
                confidence=0.5,
            )
        methods_by_file[file.relative] = file_methods

    for file in files:
        if file.absolute.suffix.lower() == ".ts":
            _extract_typescript_literals(
                builder,
                file,
                source=source,
                repository=repository,
                system_key=system_key,
                classes=classes,
                methods=methods_by_file.get(file.relative, []),
                project_id=project_id,
            )
        elif file.absolute.suffix.lower() == ".html":
            _extract_template_literals(
                builder,
                file,
                source=source,
                repository=repository,
                system_key=system_key,
                project_id=project_id,
            )
    builder.write(Path(config["output"]).resolve())


def _extract_typescript_literals(
    builder: PackageBuilder,
    file,
    *,
    source: str,
    repository: str,
    system_key: str,
    classes: dict[str, str],
    methods: list[tuple[int, str]],
    project_id: str,
) -> None:
    for match in _ROUTE_RE.finditer(file.text):
        _, route = normalize_http_route("GET", "/" + match.group("path").strip("/"))
        route_id = stable_node_id("screen", source, route)
        builder.add_node(
            route_id,
            "SCREEN",
            match.group("component"),
            f"{source}.{route}",
            route,
            system_key=system_key,
            repository_key=repository,
            confidence=0.5,
            properties={"route": route, "fallback": True},
        )
        component_id = classes.get(match.group("component"))
        if component_id:
            edge_id = builder.add_edge(
                route_id,
                component_id,
                "ROUTES_TO",
                confidence=0.5,
            )
            line = line_for_offset(file.text, match.start())
            builder.add_evidence(
                "EDGE",
                edge_id,
                file.relative,
                line,
                line,
                "ROUTE_LITERAL",
                line_text(file.text, line),
                confidence=0.5,
            )

    for match in _HTTP_RE.finditer(file.text):
        raw_method = match.group("method").upper()
        argument = match.group("argument").strip()
        literal = _literal_value(argument)
        owner_id = _nearest_method(methods, match.start()) or project_id
        line = line_for_offset(file.text, match.start())
        if not literal:
            builder.add_issue(
                "DYNAMIC_CONFIG_KEY",
                "WARNING",
                "Dynamic Angular HTTP URL was not linked",
                source_node_id=owner_id,
                raw_reference=argument[:200],
                source_path=file.relative,
                start_line=line,
                properties={"fallback": "angular-fallback"},
            )
            continue
        method = raw_method
        route_literal = literal
        if raw_method == "REQUEST":
            request_method, separator, request_path = literal.partition(" ")
            if not separator or not request_method.isalpha():
                builder.add_issue(
                    "DYNAMIC_CONFIG_KEY",
                    "WARNING",
                    "Http.request method is not a static METHOD path literal",
                    source_node_id=owner_id,
                    raw_reference=literal,
                    source_path=file.relative,
                    start_line=line,
                )
                continue
            method, route_literal = request_method.upper(), request_path
        try:
            method, route = normalize_http_route(method, route_literal)
        except ValueError:
            continue
        call_id = api_call_id(source, method, route)
        builder.add_node(
            call_id,
            "API_CALL_REFERENCE",
            f"{method} {route}",
            f"{method} {route}",
            f"{method} {route}",
            system_key=system_key,
            repository_key=repository,
            confidence=0.5,
            properties={
                "method": method,
                "route": route,
                "fallback": True,
            },
        )
        edge_id = builder.add_edge(
            owner_id,
            call_id,
            "CALLS",
            raw_operation=method,
            confidence=0.5,
        )
        builder.add_evidence(
            "EDGE",
            edge_id,
            file.relative,
            line,
            line,
            "HTTP_LITERAL",
            line_text(file.text, line),
            confidence=0.5,
        )


def _extract_template_literals(
    builder: PackageBuilder,
    file,
    *,
    source: str,
    repository: str,
    system_key: str,
    project_id: str,
) -> None:
    for match in _EVENT_RE.finditer(file.text):
        line = line_for_offset(file.text, match.start())
        action_id = stable_node_id(
            "ui-action",
            source,
            file.relative,
            match.group("event").lower(),
            str(line),
        )
        builder.add_node(
            action_id,
            "UI_ACTION",
            match.group("handler"),
            f"{file.relative}:{line}:{match.group('event')}",
            match.group("handler"),
            system_key=system_key,
            repository_key=repository,
            confidence=0.5,
            properties={"event": match.group("event").lower(), "fallback": True},
        )
        builder.add_edge(
            project_id,
            action_id,
            "CONTAINS",
            graph_layer="STRUCTURAL",
            confidence=0.5,
        )
        builder.add_evidence(
            "NODE",
            action_id,
            file.relative,
            line,
            line,
            "TEMPLATE_EVENT",
            line_text(file.text, line),
            confidence=0.5,
        )
    for match in _ROUTER_LINK_RE.finditer(file.text):
        route = _literal_router_link(match.group("route"))
        if not route:
            continue
        _, normalized = normalize_http_route("GET", route)
        route_id = stable_node_id("screen", source, normalized)
        builder.add_node(
            route_id,
            "SCREEN",
            normalized,
            f"{source}.{normalized}",
            normalized,
            system_key=system_key,
            repository_key=repository,
            confidence=0.5,
            properties={"route": normalized, "fallback": True},
        )


def _load_config(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    raw = os.path.expandvars(resolved.read_text(encoding="utf-8"))
    if "${" in raw:
        raise ValueError("Config contains unresolved environment variables")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Config root must be an object")
    return value


def _nearest_owner(
    owners: list[tuple[int, str, str]], offset: int
) -> tuple[int, str, str] | None:
    candidates = [owner for owner in owners if owner[0] <= offset]
    return candidates[-1] if candidates else None


def _nearest_method(methods: list[tuple[int, str]], offset: int) -> str:
    candidates = [method_id for start, method_id in methods if start <= offset]
    return candidates[-1] if candidates else ""


def _literal_value(expression: str) -> str:
    match = _QUOTED_RE.match(expression.strip())
    if not match:
        return ""
    return match.group(2)


def _literal_router_link(value: str) -> str:
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        parts = re.findall(r"['\"]([^'\"]+)['\"]", raw)
        return "/" + "/".join(part.strip("/") for part in parts) if parts else ""
    return raw


def _first_group(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.search(value)
    return match.group(1) if match else ""


if __name__ == "__main__":
    raise SystemExit(main())
