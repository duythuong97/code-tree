#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.contract.graph_contract import normalize_http_route, stable_node_id
from code_tree_exporter.extractors.package_support.package_writer import (
    PackageBuilder,
    api_call_id,
    configured_files,
    line_for_offset,
    line_text,
    normalize_api_call_route,
    slug,
)

_VERSION = "1.0.0"
_ROUTE_RE = re.compile(r"path\s*:\s*['\"]([^'\"]+)['\"]\s*,\s*component\s*:\s*['\"]?([A-Za-z_]\w*)['\"]?", re.IGNORECASE)
_CLASS_RE = re.compile(r"export\s+class\s+([A-Za-z_]\w*)")
_HTTP_RE = re.compile(r"this\.http\.(get|post|put|patch|delete|request)\s*\((?P<arg>[^\n;]+?)\)", re.IGNORECASE | re.DOTALL)
_EVENT_RE = re.compile(r"\((click|submit|change)\)\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_CONFIG_PROP_RE = re.compile(r"this\.config\.([A-Za-z_]\w*)")
_CONFIG_INDEX_LITERAL_RE = re.compile(r"this\.config\[['\"]([^'\"]+)['\"]\]")
_CONFIG_INDEX_DYNAMIC_RE = re.compile(r"this\.config\[([A-Za-z_]\w*)\]")
_OBJECT_CONFIG_RE = re.compile(r"(?:export\s+const\s+)?(?:orderApiConfig|apiConfig|appConfig)\s*=\s*\{(?P<body>.*?)\}\s*;", re.DOTALL)
_OBJECT_PAIR_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*['\"]([^'\"]+)['\"]")
_LITERAL_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_METHOD_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\([^)]*\)\s*\{")
_SERVICE_CALL_RE = re.compile(r"this\.service\.([A-Za-z_]\w*)\s*\(")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Angular routes, UI actions, and API calls into a CSV graph package.")
    parser.add_argument("--config", required=True, help="Path to Angular extractor JSON config")
    args = parser.parse_args()
    if os.environ.get("CODEMAP_USE_LEGACY_SCANNERS") != "1":
        return subprocess.call(["node", str(Path(__file__).with_name("main.mjs")), "--config", str(Path(args.config).expanduser())])
    config = _load_angular_config(Path(args.config).expanduser())
    extract(config)
    return 0

def _load_angular_config(path: Path) -> dict:
    path = path.expanduser().resolve()
    text = os.path.expandvars(path.read_text(encoding="utf-8"))
    unresolved = re.findall(r"\$\{[A-Z_]+\}", text)
    if unresolved:
        raise ValueError(f"Unresolved config variables: {', '.join(sorted(set(unresolved)))}")
    config = json.loads(text)
    missing = [key for key in ("type", "source", "root", "folders", "appConfig", "output") if key not in config]
    if missing:
        raise ValueError(f"Missing Angular extractor config keys: {', '.join(missing)}")
    if config["type"] != "angular":
        raise ValueError(f"Unsupported Angular extractor config type: {config['type']}")
    for key in ("root", "output", "inputData"):
        if config.get(key):
            value = Path(config[key]).expanduser()
            config[key] = str((value if value.is_absolute() else path.parent / value).resolve())
    return config


def extract(config: dict) -> None:
    source = config["source"]
    repository = config.get("repository", source)
    system_key = config.get("system", source)
    output = Path(config["output"]).resolve()
    files = configured_files(config, [".ts", ".html"])
    app_config = _load_app_config(config)
    for file in files:
        app_config.update(_inline_config(file.text))

    builder = PackageBuilder(
        f"angular-{source}",
        f"extractor:angular/{source}",
        "angular-extractor",
        _VERSION,
        {
            "source": source,
            "technology": "Python regex legacy scanner",
            "semanticTree": "unavailable",
            "degraded": True,
        },
    )
    builder.files_scanned = len(files)

    project_id = stable_node_id("angular-project", repository, source)
    builder.add_node(project_id, "ANGULAR_PROJECT", source, source, source, system_key=system_key, repository_key=repository, graph_role="TECHNICAL")
    builder.add_issue(
        "SEMANTIC_TREE_UNAVAILABLE",
        "WARNING",
        "Legacy Angular regex fallback cannot preserve nested behavior; use Node.js with TypeScript Compiler API",
        source_node_id=project_id,
        source_path=files[0].relative if files else "",
        start_line=1,
        properties={"degraded": True, "requiredParser": "typescript.createSourceFile"},
    )

    class_to_screen: dict[str, str] = {}
    screen_by_route: dict[str, str] = {}
    first_screen = ""
    for file in files:
        for match in _ROUTE_RE.finditer(file.text):
            route = "/" + match.group(1).strip("/")
            _, normalized_route = normalize_http_route("GET", route)
            component = match.group(2)
            screen_id = stable_node_id("screen", source, normalized_route)
            class_to_screen[component] = screen_id
            screen_by_route[normalized_route] = screen_id
            first_screen = first_screen or screen_id
            display = _display_from_route(normalized_route)
            builder.add_node(screen_id, "SCREEN", component, f"{source}.{normalized_route}", display, system_key=system_key, repository_key=repository, graph_role="MAIN", properties={"route": normalized_route})
            route_line = line_for_offset(file.text, match.start())
            builder.add_evidence("NODE", screen_id, file.relative, route_line, route_line, "DECLARATION", line_text(file.text, route_line))
            builder.add_edge(project_id, screen_id, "PROJECT_REFERENCE")
            component_id = stable_node_id("angular-component", source, slug(component.removesuffix("Component")))
            builder.add_node(component_id, "ANGULAR_COMPONENT", component, f"{source}.{component}", component, system_key=system_key, repository_key=repository, graph_role="TECHNICAL")
            builder.add_edge(screen_id, component_id, "CONTAINS", graph_layer="STRUCTURAL")

    service_ids: dict[str, str] = {}
    method_to_api: dict[str, list[str]] = {}
    for file in files:
        classes = list(_CLASS_RE.finditer(file.text))
        for index, cls in enumerate(classes):
            class_name = cls.group(1)
            end = classes[index + 1].start() if index + 1 < len(classes) else len(file.text)
            body = file.text[cls.start():end]
            if "HttpClient" in body or "this.http" in body:
                service_id = stable_node_id("angular-service", source, slug(class_name.removesuffix("Service")))
                service_ids[class_name] = service_id
                builder.add_node(service_id, "ANGULAR_SERVICE", class_name, f"{source}.{class_name}", class_name, system_key=system_key, repository_key=repository, graph_role="TECHNICAL")
                class_line = line_for_offset(file.text, cls.start())
                builder.add_evidence("NODE", service_id, file.relative, class_line, line_for_offset(file.text, end), "DECLARATION", line_text(file.text, class_line))
                for http in _HTTP_RE.finditer(body):
                    method = http.group(1).upper()
                    expression = http.group("arg")
                    route, issue = _resolve_url(expression, app_config)
                    line = line_for_offset(file.text, cls.start() + http.start())
                    if issue:
                        builder.add_issue("DYNAMIC_CONFIG_KEY", "WARNING", "Runtime configuration key cannot be resolved", source_node_id=service_id, raw_reference=issue, source_path=file.relative, start_line=line)
                        continue
                    if not route:
                        continue
                    call_id = api_call_id(source, method, route)
                    _, normalized_route = normalize_http_route(method, route)
                    builder.add_node(call_id, "API_CALL_REFERENCE", f"{method} {normalized_route}", f"{method} {normalized_route}", f"{method} {normalized_route}", system_key=system_key, repository_key=repository, graph_role="TECHNICAL", properties={"method": method, "route": normalized_route})
                    edge_id = builder.add_edge(service_id, call_id, "CALLS")
                    builder.add_evidence("EDGE", edge_id, file.relative, line, line, "HTTP_CALL", line_text(file.text, line))
                    owner_method = _enclosing_method(body, http.start())
                    if owner_method:
                        method_to_api.setdefault(owner_method, []).append(call_id)

    for file in files:
        for event in _EVENT_RE.finditer(file.text):
            action_method = event.group(2).split("(", 1)[0].strip()
            action_id = stable_node_id("ui-action", source, slug(action_method))
            screen_id = first_screen or next(iter(screen_by_route.values()), "")
            if not screen_id:
                continue
            builder.add_node(action_id, "UI_ACTION", action_method, f"{source}.{action_method}", _display_from_method(action_method), system_key=system_key, repository_key=repository, graph_role="MAIN")
            edge_id = builder.add_edge(screen_id, action_id, "CONTAINS", graph_layer="STRUCTURAL")
            line = line_for_offset(file.text, event.start())
            builder.add_evidence("NODE", action_id, file.relative, line, line, "DECLARATION", event.group(0))
            builder.add_evidence("EDGE", edge_id, file.relative, line, line, "TEMPLATE_EVENT", event.group(0))

        for cls in _CLASS_RE.finditer(file.text):
            class_name = cls.group(1)
            screen_id = class_to_screen.get(class_name)
            if not screen_id:
                continue
            block_end = _next_class_start(file.text, cls.end())
            body = file.text[cls.start():block_end]
            for method in _METHOD_RE.finditer(body):
                action_method = method.group(1)
                method_body = body[method.end(): _method_end(body, method.end())]
                action_id = stable_node_id("ui-action", source, slug(action_method))
                linked = False
                for service_call in _SERVICE_CALL_RE.finditer(method_body):
                    for api_id in method_to_api.get(service_call.group(1), []):
                        builder.add_node(action_id, "UI_ACTION", action_method, f"{source}.{action_method}", _display_from_method(action_method), system_key=system_key, repository_key=repository, graph_role="MAIN")
                        method_line = line_for_offset(file.text, cls.start() + method.start())
                        builder.add_evidence("NODE", action_id, file.relative, method_line, method_line, "DECLARATION", line_text(file.text, method_line))
                        builder.add_edge(screen_id, action_id, "CONTAINS", graph_layer="STRUCTURAL")
                        builder.add_edge(action_id, api_id, "CALLS")
                        linked = True
                if linked:
                    continue

    _route_quality_issues(config, builder, files, source, system_key)
    builder.write(output)


def _load_app_config(config: dict) -> dict[str, str]:
    path_value = config.get("appConfig")
    if not path_value:
        return {}
    value = Path(path_value).expanduser()
    path = value if value.is_absolute() else Path(config["root"]) / value
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, str)}


def _inline_config(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for obj in _OBJECT_CONFIG_RE.finditer(text):
        for pair in _OBJECT_PAIR_RE.finditer(obj.group("body")):
            values[pair.group(1)] = pair.group(2)
    return values


def _resolve_url(expression: str, config: dict[str, str]) -> tuple[str, str]:
    dynamic = _CONFIG_INDEX_DYNAMIC_RE.search(expression)
    literal_index = _CONFIG_INDEX_LITERAL_RE.search(expression)
    prop = _CONFIG_PROP_RE.search(expression)
    if dynamic and not literal_index:
        return "", f"this.config[{dynamic.group(1)}]"
    key = literal_index.group(1) if literal_index else prop.group(1) if prop else ""
    if key:
        route = config.get(key, "")
        return route if route else "", "" if route else f"this.config.{key}"
    literal = _LITERAL_RE.search(expression)
    if literal:
        return literal.group(1), ""
    return "", expression.strip()

def _target_from_api_call_id(call_id: str) -> str:
    parts = call_id.split(":", 3)
    if len(parts) < 4:
        return ""
    _, _source, method, route = parts
    return _api_operation_from_prefixed_route(method, route)


def _route_quality_issues(config: dict, builder: PackageBuilder, files, source: str, system_key: str) -> None:
    checks = config.get("routeChecks", [])
    if not checks:
        return
    text_by_path = {file.relative: file.text for file in files}
    for check in checks:
        raw = check.get("rawReference", "")
        path = check.get("sourcePath", "")
        source_path = path if path else (files[0].relative if files else "")
        text = text_by_path.get(source_path, files[0].text if files else "")
        line = _line_containing(text, raw) or check.get("line")
        node_id = check.get("sourceNodeId", "")
        if not node_id and check.get("apiCall"):
            method, route = check["apiCall"].split(" ", 1)
            node_id = api_call_id(source, method, route)
        builder.add_issue(check["issueType"], check.get("severity", "WARNING"), check.get("message", "Route could not be resolved"), source_node_id=node_id, raw_reference=raw, database_key=check.get("database", ""), source_path=source_path, start_line=int(line or 1))


def _line_containing(text: str, needle: str) -> int:
    if not needle:
        return 0
    for index, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return index
    return 0


def _display_from_route(route: str) -> str:
    parts = [part for part in route.strip("/").split("/") if part and part != "{id}"]
    return " ".join(part.capitalize() for part in parts) or "Home"


def _display_from_method(name: str) -> str:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).replace("_", " ")
    return words[:1].upper() + words[1:]


def _next_class_start(text: str, start: int) -> int:
    match = _CLASS_RE.search(text, start)
    return match.start() if match else len(text)


def _method_end(text: str, start: int) -> int:
    depth = 1
    index = start
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text)


def _enclosing_method(block: str, offset: int) -> str:
    current = ""
    for match in _METHOD_RE.finditer(block[:offset]):
        current = match.group(1)
    return current


if __name__ == "__main__":
    raise SystemExit(main())
